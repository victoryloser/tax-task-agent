import sqlite3

import pytest
from pydantic import ValidationError

from tax_agent.domain.authorization import Authorization
from tax_agent.domain.finance import Finance
from tax_agent.domain.writes import Writes
from tax_agent.models import BusinessError, UpdateInvoice
from tax_agent.storage.db import uid
from tax_agent.tools.executor import ToolExecutor
from conftest import AllowAuthorization, message


def args(ctx,invoice_id="F002",value="客户培训资料打印",version=0,quote=None):
    return UpdateInvoice(invoice_id=invoice_id,expected_version=version,patch={"purpose":value},
        source_refs=[{"message_id":ctx.message_id,"quote":quote or f"请将{invoice_id}的用途保存为{value}"}])


def test_save_verify_and_replay_keep_same_business_version(db,ctx,authorization):
    service=Writes(db,authorization)
    request=args(ctx)
    result=service.update(ctx,request)
    assert result["state"]=="verified" and result["current"]["purpose"]=="客户培训资料打印"
    assert result["row_version"]==1
    repeated=service.update(ctx,request.model_copy(update={"expected_version":1}))
    assert repeated["operation_id"]==result["operation_id"] and repeated["row_version"]==1
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1
        assert conn.execute("SELECT purpose,row_version FROM invoices WHERE enterprise_id='C002' AND invoice_id='F002'").fetchone()[:]==(None,0)


def test_request_patch_conflict_and_new_turn_correction(db,ctx,authorization):
    service=Writes(db,authorization)
    service.update(ctx,args(ctx))
    with pytest.raises(BusinessError,match="不能更换"): service.update(ctx,args(ctx,value="另一个用途"))
    session={"session_id":ctx.session_id}
    new=message(db,"请将F002的用途保存为项目培训材料",session=session)
    corrected=service.update(new,args(new,value="项目培训材料",version=1))
    assert corrected["row_version"]==2 and corrected["current"]["purpose"]=="项目培训材料"


def test_version_conflict_is_not_overwritten(db,ctx,authorization):
    with db.transaction(True) as conn: conn.execute("UPDATE invoices SET purpose='他人修改',row_version=1 WHERE enterprise_id='C001' AND invoice_id='F002'")
    with pytest.raises(BusinessError,match="版本已变化"): Writes(db,authorization).update(ctx,args(ctx))
    assert Finance(db).get(ctx,"F002")["purpose"]=="他人修改"


@pytest.mark.parametrize("mode",["fake_id","assistant","tool","wrong_quote","wrong_scope","missing_current"])
def test_sources_must_be_current_real_user_messages(db,ctx,authorization,mode):
    request=args(ctx)
    if mode=="fake_id":
        request.source_refs[0].message_id=uid()
    elif mode in {"assistant","tool"}:
        other=message(db,"请将F002的用途保存为客户培训资料打印",role=mode)
        request.source_refs[0].message_id=other.message_id
    elif mode=="wrong_quote":
        request.source_refs[0].quote="根本没说过的用途"
    elif mode=="wrong_scope":
        other=message(db,"请将F002的用途保存为客户培训资料打印",enterprise="C002")
        request.source_refs[0].message_id=other.message_id
    else:
        other=message(db,"先别保存",session={"session_id":ctx.session_id})
        ctx=other
    with pytest.raises(BusinessError): Writes(db,authorization).update(ctx,request)
    assert Finance(db).get(ctx,"F002")["row_version"]==0


def test_readonly_user_cannot_save_or_inspect_other_user_operation(db,ctx,authorization):
    operation=Writes(db,authorization).update(ctx,args(ctx))
    readonly=message(db,"请将F002的用途保存为客户培训资料打印",user="U002")
    with pytest.raises(BusinessError,match="无权"): Writes(db,authorization).update(readonly,args(readonly))
    with pytest.raises(BusinessError,match="无权"): Writes(db,authorization).verify(readonly,operation["operation_id"])


@pytest.mark.parametrize("extra",[{"amount_cents":1},{"invoice_date":"2026-08-01"},{"bank_amount":5},{"authorized":True}])
def test_write_contract_disallows_business_or_authorization_fields(extra,ctx):
    raw=args(ctx).model_dump()
    raw["patch"].update(extra)
    with pytest.raises(ValidationError): UpdateInvoice.model_validate(raw)


def test_exact_values_clear_and_unchanged_value(db,authorization):
    ctx=message(db,"请将F002的用途保存为  培训资料  ")
    service=Writes(db,authorization)
    request=args(ctx,value="  培训资料  ")
    result=service.update(ctx,request)
    assert result["current"]["purpose"]=="  培训资料  "
    other=message(db,"请将F002的用途保存为  培训资料  ",session={"session_id":ctx.session_id})
    unchanged=service.update(other,args(other,value="  培训资料  ",version=1))
    assert unchanged["changed"] is False and unchanged["row_version"]==1
    clear=message(db,"请清空F002的用途",session={"session_id":ctx.session_id})
    result=service.update(clear,args(clear,value="",version=1,quote="请清空F002的用途"))
    assert result["current"]["purpose"]=="" and result["row_version"]==2
    assert "F002" in [r["invoice_id"] for r in Finance(db).missing(clear,"2026-09")["items"]]


def test_submitted_commit_is_recovered_without_second_update(db,ctx,authorization,monkeypatch):
    service=Writes(db,authorization)
    def lost_response(_): raise sqlite3.OperationalError("lost commit response")
    monkeypatch.setattr(service,"_after_commit",lost_response)
    result=service.update(ctx,args(ctx))
    assert result["state"]=="verified" and result["row_version"]==1


def test_readback_failure_pauses_other_writes_then_verifies(db,ctx,monkeypatch):
    executor=ToolExecutor(db,ctx,AllowAuthorization())
    original=executor.writes.finance.get
    committed=False
    def after_commit(_):
        nonlocal committed
        committed=True
    monkeypatch.setattr(executor.writes,"_after_commit",after_commit)
    def fail_readback(*a,**kw):
        if committed: raise sqlite3.OperationalError("read failed")
        return original(*a,**kw)
    monkeypatch.setattr(executor.writes.finance,"get",fail_readback)
    result=executor.execute("update_invoice",args(ctx).model_dump(exclude_unset=True))
    assert result["status"]=="committed_unverified"
    paused=executor.execute("update_invoice",args(ctx,invoice_id="F003").model_dump(exclude_unset=True))
    assert paused["status"]=="outcome_unknown"
    monkeypatch.setattr(executor.writes.finance,"get",original)
    verified=executor.execute("inspect_operation",{"operation_id":result["data"]["operation_id"]})
    assert verified["status"]=="ok"
    assert Finance(db).get(ctx,"F002")["row_version"]==1 and Finance(db).get(ctx,"F003")["row_version"]==0


def test_later_change_does_not_trigger_rewrite(db,ctx,authorization):
    service=Writes(db,authorization)
    result=service.update(ctx,args(ctx))
    with db.transaction(True) as conn: conn.execute("UPDATE invoices SET purpose='后续更正',row_version=2 WHERE enterprise_id='C001' AND invoice_id='F002'")
    checked=service.verify(ctx,result["operation_id"])
    assert checked["state"]=="superseded" and checked["current"]["purpose"]=="后续更正"


def test_partial_success_keeps_saved_item(db,authorization):
    ctx=message(db,"请将F002的用途保存为培训；请将F003的用途保存为会议")
    executor=ToolExecutor(db,ctx,AllowAuthorization())
    first=args(ctx,value="培训",quote="请将F002的用途保存为培训")
    second=args(ctx,invoice_id="F003",value="会议",version=99,quote="请将F003的用途保存为会议")
    assert executor.execute("update_invoice",first.model_dump(exclude_unset=True))["status"]=="ok"
    assert executor.execute("update_invoice",second.model_dump(exclude_unset=True))["status"]=="conflict"
    assert Finance(db).get(ctx,"F002")["purpose"]=="培训" and Finance(db).get(ctx,"F003")["purpose"] is None


def test_unknown_commit_outcome_pauses_until_ledger_can_be_read(db,ctx,monkeypatch):
    executor=ToolExecutor(db,ctx,AllowAuthorization())
    original=db.transaction
    def after_commit(_):
        def fail_probe(*a,**kw):
            monkeypatch.setattr(db,"transaction",original)
            raise sqlite3.OperationalError("temporarily unavailable")
        monkeypatch.setattr(db,"transaction",fail_probe)
        raise sqlite3.OperationalError("commit response lost")
    monkeypatch.setattr(executor.writes,"_after_commit",after_commit)
    result=executor.execute("update_invoice",args(ctx).model_dump(exclude_unset=True))
    assert result["status"]=="outcome_unknown" and executor.write_paused
    assert Finance(db).get(ctx,"F002")["row_version"]==1
    from tax_agent.context.builder import build_messages
    other=message(db,"核实上一次保存")
    messages,_=build_messages(db,other)
    assert result["data"]["operation_id"] in messages[0]["content"]
    isolated=message(db,"核实上一次保存",enterprise="C002")
    messages,_=build_messages(db,isolated)
    assert result["data"]["operation_id"] not in messages[0]["content"]
    verified=executor.execute("inspect_operation",{"operation_id":result["data"]["operation_id"]})
    assert verified["status"]=="ok" and Finance(db).get(ctx,"F002")["row_version"]==1
