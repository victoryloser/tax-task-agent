from conftest import message
from tax_agent.runtime.legacy import reconcile
from tax_agent.domain.writes import Writes
from test_writes import args


def test_legacy_committed_write_is_verified_without_rewrite(db,authorization):
    ctx=message(db,"请将F002的用途保存为客户培训资料打印")
    saved=Writes(db,authorization).update(ctx,args(ctx))
    report=reconcile(db)
    assert len(report)==1 and report[0]["operation_ids"]==[saved["operation_id"]]
    result=reconcile(db,apply=True)[0]
    assert result["readbacks"][0]["state"]=="verified"
    with db.transaction() as conn:
        assert conn.execute("SELECT row_version FROM invoices WHERE enterprise_id='C001' AND invoice_id='F002'").fetchone()[0]==1
        assert conn.execute("SELECT status FROM turns WHERE turn_id=?",(ctx.turn_id,)).fetchone()[0]=="incomplete"
    assert reconcile(db,apply=True)==[]


def test_legacy_unknown_memory_call_is_retained_not_reexecuted(db):
    ctx=message(db,"长期记住表格")
    db.event(ctx,"tool_call",{"name":"preference_manage","arguments":{"action":"create","key":"format","value":"表格"}})
    result=reconcile(db,apply=True)[0]
    assert len(result["unresolved_calls"])==1
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]==0
