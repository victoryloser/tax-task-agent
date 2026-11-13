import json

import pytest

from tax_agent.context.builder import build_messages
from tax_agent.domain.memory import Memory
from tax_agent.domain.writes import Writes
from tax_agent.models import BusinessError, PreferenceManage, SourceRef, TaskManage
from tax_agent.storage.db import uid
from conftest import message
from test_writes import args as write_args


def source(ctx,quote):
    return [SourceRef(message_id=ctx.message_id,quote=quote)]


def start_task(db,authorization):
    ctx=message(db,"检查2026年9月哪些发票缺用途，并持续办理")
    task=Memory(db,authorization).manage_task(ctx,TaskManage(action="create",month="2026-09",goal="补齐9月用途",source_refs=source(ctx,"检查2026年9月哪些发票缺用途，并持续办理")))["task"]
    return ctx,task


def test_one_unfinished_task_partial_save_new_session_resume(db,authorization):
    ctx,task=start_task(db,authorization)
    assert task["pending"]["invoice_ids"]==["F002","F003","F004","F006","F009"]
    other=message(db,"检查2026年9月缺用途",session={"session_id":ctx.session_id})
    with pytest.raises(BusinessError,match="已有未完成"):
        Memory(db,authorization).manage_task(other,TaskManage(action="create",month="2026-09",goal="重复任务",source_refs=source(other,"检查2026年9月缺用途")))
    update=message(db,"请将F002的用途保存为客户培训资料打印",session={"session_id":ctx.session_id})
    request=write_args(update).model_copy(update={"task_id":task["task_id"],"task_revision":task["revision"]})
    result=Writes(db,authorization).update(update,request)
    assert result["task"]["pending"]["invoice_ids"]==["F003","F004","F006","F009"]
    resumed=message(db,"继续原来的检查任务")
    current=Memory(db).tasks(resumed,False)[0]
    task=Memory(db,authorization).manage_task(resumed,TaskManage(action="resume",task_id=current["task_id"],expected_revision=current["revision"],source_refs=source(resumed,"继续原来的检查任务")))["task"]
    assert task["pending"]["invoice_ids"]==["F003","F004","F006","F009"] and task["done"]["F002"]==result["operation_id"]
    with db.transaction() as conn:
        assert conn.execute("SELECT row_version FROM invoices WHERE enterprise_id='C001' AND invoice_id='F002'").fetchone()[0]==1


def test_completion_rechecks_business_and_revision(db,authorization):
    ctx,task=start_task(db,authorization)
    end=message(db,"完成检查",session={"session_id":ctx.session_id})
    action=TaskManage(action="complete",task_id=task["task_id"],expected_revision=task["revision"],source_refs=source(end,"完成检查"))
    with pytest.raises(BusinessError,match="仍有未完成"): Memory(db,authorization).manage_task(end,action)
    with db.transaction(True) as conn:
        conn.execute("UPDATE invoices SET purpose='测试已核实用途' WHERE enterprise_id='C001' AND invoice_date LIKE '2026-09%'")
    result=Memory(db,authorization).manage_task(end,action)["task"]
    assert result["status"]=="COMPLETED" and result["pending"]["invoice_ids"]==[]


@pytest.mark.parametrize("action",["cancel","delete"])
def test_cancel_delete_keeps_business_and_invalidates_old_request(db,authorization,action):
    ctx,task=start_task(db,authorization)
    old_session=db.session(ctx.session_id,ctx.user_id,ctx.enterprise_id)
    change=message(db,"取消并删除这个任务",session=old_session)
    Memory(db,authorization).manage_task(change,TaskManage(action=action,task_id=task["task_id"],expected_revision=task["revision"],source_refs=source(change,"取消并删除这个任务")))
    assert not Memory(db).tasks(change,False)
    new_session=db.session(ctx.session_id,ctx.user_id,ctx.enterprise_id)
    assert new_session["thread_id"]!=old_session["thread_id"] and new_session["memory_epoch"]>old_session["memory_epoch"]
    with db.transaction() as conn: assert conn.execute("SELECT SUM(row_version) FROM invoices").fetchone()[0]==0
    continuation=message(db,"继续",session=old_session)
    with pytest.raises(BusinessError):
        authorization.sources(continuation,source(ctx,"检查2026年9月哪些发票缺用途，并持续办理")+source(continuation,"继续"))


def test_task_replacement_update_and_old_revision(db,authorization):
    ctx,task=start_task(db,authorization)
    update=message(db,"替换为补充2026年8月F011发票用途",session={"session_id":ctx.session_id})
    new=Memory(db,authorization).manage_task(update,TaskManage(action="replace",task_id=task["task_id"],expected_revision=task["revision"],month="2026-08",goal="补充F011用途",kind="invoice_update",invoice_ids=["F011"],source_refs=source(update,"替换为补充2026年8月F011发票用途")))["task"]
    assert new["task_id"]!=task["task_id"] and new["pending"]["invoice_ids"]==["F011"]
    assert Memory(db).tasks(update)[0]["status"]=="SUPERSEDED"
    changed=message(db,"改为补充2026年8月F012",session={"session_id":ctx.session_id})
    revised=Memory(db,authorization).manage_task(changed,TaskManage(action="update",task_id=new["task_id"],expected_revision=new["revision"],month="2026-08",goal="补充F012",kind="invoice_update",invoice_ids=["F012"],source_refs=source(changed,"改为补充2026年8月F012")))["task"]
    assert revised["revision"]>new["revision"] and revised["pending"]["invoice_ids"]==["F012"]


def test_task_scope_and_preferences_do_not_cross_user_or_enterprise(db,authorization):
    ctx,task=start_task(db,authorization)
    for user,enterprise in [("U001","C002"),("U002","C001")]:
        other=message(db,"查看任务",user=user,enterprise=enterprise)
        assert Memory(db).tasks(other)==[]
        with pytest.raises(BusinessError):
            Memory(db,authorization).manage_task(other,TaskManage(action="get",task_id=task["task_id"]))


def test_preferences_crud_epoch_old_browser_and_partial_source_invalidation(db,authorization):
    content="以后回答简洁些；检查2026年9月缺用途"
    ctx=message(db,content)
    memory=Memory(db,authorization)
    preference=memory.manage_preference(ctx,PreferenceManage(action="create",key="回答方式",value="简洁些",source_refs=source(ctx,"以后回答简洁些")))
    memory_id=preference["memory_id"]
    task=memory.manage_task(ctx,TaskManage(action="create",month="2026-09",goal="检查缺用途",source_refs=source(ctx,"检查2026年9月缺用途")))["task"]
    old=db.session(ctx.session_id,ctx.user_id,ctx.enterprise_id)
    fresh=message(db,"查看偏好")
    assert Memory(db).preferences(fresh)[0]["value"]=="简洁些"
    delete=message(db,"删除回答方式偏好",session=old)
    memory.manage_preference(delete,PreferenceManage(action="delete",memory_id=memory_id,expected_revision=1,source_refs=source(delete,"删除回答方式偏好")))
    assert Memory(db).preferences(fresh)==[]
    restored=db.session(old["session_id"],"U001","C001")
    assert restored["thread_id"]!=old["thread_id"]
    current=message(db,"继续检查",session=old)
    messages,_=build_messages(db,current)
    assert "简洁些" not in json.dumps(messages,ensure_ascii=False)
    assert task["task_id"] in json.dumps(messages)
    # 同条原消息中的任务片段仍有效；被删除的偏好片段不可再抽取。
    authorization.sources(current,source(ctx,"检查2026年9月缺用途")+source(current,"继续检查"))
    with pytest.raises(BusinessError,match="已删除"):
        authorization.sources(current,source(ctx,"以后回答简洁些")+source(current,"继续检查"))


def test_preference_update_preserves_current_value_and_blocks_stale_revision(db,authorization):
    ctx=message(db,"以后回答简短")
    memory=Memory(db,authorization)
    result=memory.manage_preference(ctx,PreferenceManage(action="create",key="风格",value="简短",source_refs=source(ctx,"以后回答简短")))
    update=message(db,"把长期偏好改为详细解释",session={"session_id":ctx.session_id})
    request=PreferenceManage(action="update",memory_id=result["memory_id"],expected_revision=1,key="风格",value="详细解释",source_refs=source(update,"把长期偏好改为详细解释"))
    changed=memory.manage_preference(update,request)
    assert changed["preferences"][0]["value"]=="详细解释" and changed["preferences"][0]["revision"]==2
    with pytest.raises(BusinessError,match="版本已变化"): memory.manage_preference(update,request)
