import json

from conftest import message
from tax_agent.context.builder import build_messages
from tax_agent.context.history import recall
from tax_agent.domain.memory import Memory
from tax_agent.models import RecallHistory


def test_old_history_compressed_with_sources_and_recall(db):
    session=db.new_session("U001","C001")
    first=message(db,"客户代号是星河，请本轮用它描述查询对象。"+"详细背景。"*100,session=session)
    for i in range(18):
        message(db,f"第{i}轮临时查询2026年9月",session=session)
    ctx=message(db,"之前提到的星河客户代号是什么",session=session)
    messages,epoch=build_messages(db,ctx)
    core=json.loads(messages[0]["content"].split("可信当前状态：\n")[1])
    summary=core["history_summary"]
    assert summary["covered_messages"]==10 and len(summary["excerpts"])==8
    excerpt=next(r for r in summary["excerpts"] if r["message_id"]==first.message_id)
    assert excerpt["truncated"] and len(excerpt["quote"])==320
    result=recall(db,ctx,RecallHistory(message_id=first.message_id))
    assert result["total_count"]==1 and result["items"][0]["content"].startswith(excerpt["quote"])
    assert len(result["items"][0]["content"])>320


def test_epoch_invalidation_removes_compaction_and_retrieval(db):
    session=db.new_session("U001","C001")
    old=message(db,"将被失效的旧偏好",session=session)
    for i in range(12): message(db,str(i),session=session)
    ctx=message(db,"本轮删除",session=session)
    build_messages(db,ctx)
    with db.transaction(True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM context_summaries").fetchone()[0]==1
        Memory(db).invalidate(ctx,"removed",[{"message_id":old.message_id,"quote":"旧偏好"}],conn)
        assert conn.execute("SELECT COUNT(*) FROM context_summaries").fetchone()[0]==0
    next_ctx=message(db,"继续",session=session)
    messages,_=build_messages(db,next_ctx)
    assert "将被失效" not in str(messages)
    assert recall(db,next_ctx,RecallHistory(message_id=old.message_id))["total_count"]==0
    other=message(db,"星河",user="U001",enterprise="C002")
    assert recall(db,other,RecallHistory(message_id=old.message_id))["total_count"]==0


def test_context_resolves_enterprise_aliases_and_actual_permissions(db):
    ctx=message(db,"查乙企业",user="U002")
    messages,_=build_messages(db,ctx)
    core=json.loads(messages[0]["content"].split("可信当前状态：\n")[1])
    companies={r["enterprise_id"]:r for r in core["enterprise_directory"]}
    assert companies["C001"]["alias"]=="甲企业" and companies["C001"]["can_read"]==1
    assert companies["C001"]["can_write"]==0
    assert companies["C002"]["alias"]=="乙企业" and companies["C002"]["can_read"]==0
