import json
import sqlite3

import pytest

from conftest import AllowAuthorization, message
from test_runtime import Scripted, call
from tax_agent.config import Settings
from tax_agent.domain.batches import Batches
from tax_agent.llm.provider import ModelReply
from tax_agent.models import AuthContext
from tax_agent.runtime.graph import Harness
from tax_agent.tools.executor import ToolExecutor
from tax_agent.storage.db import dumps, uid


class BatchProvider:
    mode="mock"
    model="batch-fixture"
    def __init__(self,targets,mode="per_invoice",execute=()):
        self.targets,self.batch_mode,self.execute=targets,mode,iter(execute)
        self.planning_calls=0
    def generate(self,messages,tools,tool_choice="auto"):
        name=tools[0]["function"]["name"]
        if name=="request_manifest":
            context=json.loads(messages[-1]["content"])["context"]
            core=json.loads(context[0]["content"].split("可信当前状态：\n")[1])
            current=next(json.loads(m["content"]) for m in reversed(context) if m["role"]=="user")
            self.refs=[{"message_id":core["current_message_id"],"quote":current["text"]}]
            targets=[t|{"source_refs":t.get("source_refs",self.refs)} for t in self.targets]
            return ModelReply(call(name,{"write_requested":True,"mode":self.batch_mode,"targets":targets,"reason":"explicit fixture"}),self.model,{})
        if name=="authorization_decision":
            return ModelReply(call(name,{"allowed":True,"reason":"fixture"}),self.model,{})
        self.planning_calls+=1
        target=next(self.execute,None)
        if target is None:
            return ModelReply({"role":"assistant","content":"只描述已办项的模型回答"},self.model,{})
        args={"invoice_id":target["invoice_id"],"expected_version":target.get("version",0),
              "patch":{f:target[f] for f in ("purpose","note") if target.get(f) is not None},
              "source_refs":target.get("source_refs",self.refs)}
        return ModelReply(call("update_invoice",args),self.model,{})


def run(db,tmp_path,provider,text):
    session=db.new_session("U001","C001")
    harness=Harness(Settings(db_path=db.path,checkpoint_path=tmp_path/"cp.sqlite3"),provider)
    return harness.respond(session["session_id"],"U001","C001",text)


def test_every_target_reported_even_when_missing_or_never_called(db,tmp_path):
    one={"invoice_id":"F002","purpose":"培训"}
    targets=[one,{"invoice_id":"F003","missing_fields":["purpose"]},{"invoice_id":"F004","purpose":"住宿"}]
    result=run(db,tmp_path,BatchProvider(targets,execute=[one]),"F002用途保存为培训，F003用途稍后补充，F004用途保存为住宿。")
    assert result.status=="incomplete"
    assert all(i in result.answer for i in ("F002","F003","F004","缺少信息","未执行"))
    with db.transaction() as conn:
        assert [r[0] for r in conn.execute("SELECT status FROM batch_items ORDER BY item_id")]==["saved","needs_input","not_executed"]
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1
    other=message(db,"继续")
    from tax_agent.context.builder import build_messages
    context,_=build_messages(db,other)
    core=json.loads(context[0]["content"].split("可信当前状态：\n")[1])
    assert {t["invoice_id"] for t in core["pending_write_targets"]}=={"F003","F004"}


def test_atomic_request_refused_before_any_invoice_write(db,tmp_path):
    targets=[{"invoice_id":"F002","purpose":"培训"},{"invoice_id":"F003","purpose":"会议"}]
    provider=BatchProvider(targets,mode="atomic",execute=targets)
    result=run(db,tmp_path,provider,"F002用途培训，F003用途会议，必须一起提交，失败则全部不改。")
    assert provider.planning_calls==0 and "全部未执行" in result.answer
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==0
        assert conn.execute("SELECT COUNT(*) FROM batch_items WHERE status='atomic_blocked'").fetchone()[0]==2
        assert conn.execute("SELECT SUM(row_version) FROM invoices").fetchone()[0]==0


def test_partial_failure_has_separate_success_failure_and_unexecuted(db,tmp_path):
    a={"invoice_id":"F002","purpose":"培训"}
    b={"invoice_id":"F003","purpose":"会议","version":99}
    targets=[a,{k:v for k,v in b.items() if k!="version"},{"invoice_id":"F004","purpose":"住宿"}]
    result=run(db,tmp_path,BatchProvider(targets,execute=[a,b]),"F002用途培训，F003用途会议，F004用途住宿，分别保存。")
    assert result.status=="incomplete"
    assert "F002：已保存" in result.answer and "F003：失败" in result.answer and "F004：未执行" in result.answer


def test_read_retries_have_independent_persisted_budget(db,ctx,monkeypatch):
    settings=Settings(read_retry_limit=1)
    executor=ToolExecutor(db,ctx,AllowAuthorization(),settings)
    count=[]
    def fail(*args):
        count.append(1)
        raise sqlite3.OperationalError("read unavailable")
    monkeypatch.setattr(executor.finance,"get",fail)
    raw={"invoice_id":"F002"}
    assert executor.execute("get_invoice",raw,"one")["status"]=="failed"
    assert executor.execute("get_invoice",raw,"two")["status"]=="failed"
    # 模拟worker恢复，预算从数据库恢复；新的tool_call_id不能绕过限制。
    recovered=ToolExecutor(db,ctx,AllowAuthorization(),settings)
    monkeypatch.setattr(recovered.finance,"get",fail)
    result=recovered.execute("get_invoice",raw|{"enterprise_id":"C001"},"three")
    assert result["error_code"]=="READ_RETRY_BUDGET" and len(count)==2
    assert recovered.execute("get_invoice",raw|{"enterprise_id":"C001"},"three")==result


def test_manifest_survives_restart_before_first_write(db,tmp_path):
    one={"invoice_id":"F002","purpose":"培训"}
    provider=BatchProvider([one],execute=[one])
    session=db.new_session("U001","C001")
    settings=Settings(db_path=db.path,checkpoint_path=tmp_path/"cp.sqlite3")
    h=Harness(settings,provider)
    original=h._save_state
    def interrupted(*args):
        original(*args)
        raise KeyboardInterrupt()
    h._save_state=interrupted
    tid=h.submit(session["session_id"],"U001","C001","F002用途保存为培训。")
    with pytest.raises(KeyboardInterrupt): h.run_once()
    recovered=Harness(settings,provider).run_once()
    assert recovered.turn_id==tid and recovered.status=="completed"
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_manifests").fetchone()[0]==1
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1


def test_cross_session_completion_supersedes_pending_target(db,tmp_path):
    first=run(db,tmp_path,BatchProvider([{"invoice_id":"F003","missing_fields":["purpose"]}]),"F003用途稍后补充。")
    target={"invoice_id":"F003","purpose":"会议资料","previous_turn_id":first.turn_id}
    second=run(db,tmp_path,BatchProvider([target],execute=[target]),"继续F003，用途保存为会议资料。")
    assert "已保存" in second.answer
    with db.transaction() as conn:
        assert conn.execute("SELECT status FROM batch_items WHERE turn_id=?",(first.turn_id,)).fetchone()[0]=="superseded"
        assert conn.execute("SELECT row_version FROM invoices WHERE enterprise_id='C001' AND invoice_id='F003'").fetchone()[0]==1


def test_batch_cancel_receipt_replayed_after_interruption(db,tmp_path):
    first=run(db,tmp_path,BatchProvider([{"invoice_id":"F003","missing_fields":["purpose"]}]),"F003用途稍后补充。")
    ctx=message(db,"取消上次F003未执行的保存请求。")
    executor=ToolExecutor(db,ctx,AllowAuthorization(),Settings())
    args={"turn_id":first.turn_id,"source_refs":[{"message_id":ctx.message_id,"quote":"取消上次F003未执行的保存请求。"}]}
    original=executor.execute("batch_cancel",args,"cancel-one")
    assert original["status"]=="ok" and original["data"]["cancelled_count"]==1
    with db.transaction() as conn:
        epoch=db.epoch(ctx.user_id,ctx.enterprise_id,conn)
    recovered=ToolExecutor(db,ctx,AllowAuthorization(),Settings()).execute("batch_cancel",args,"cancel-one")
    assert recovered==original
    with db.transaction() as conn:
        assert db.epoch(ctx.user_id,ctx.enterprise_id,conn)==epoch
        assert conn.execute("SELECT status FROM batch_items WHERE turn_id=?",(first.turn_id,)).fetchone()[0]=="cancelled"


def test_deleted_preference_does_not_reappear_via_pending_target(db,tmp_path):
    from tax_agent.context.builder import build_messages
    from tax_agent.domain.memory import Memory
    first=run(db,tmp_path,BatchProvider([{"invoice_id":"F003","missing_fields":["purpose"]}]),"以后称呼我小明。F003用途稍后补充。")
    ctx=message(db,"删除称呼偏好")
    with db.transaction(True) as conn:
        mid=conn.execute("SELECT message_id FROM turns WHERE turn_id=?",(first.turn_id,)).fetchone()[0]
        Memory(db).invalidate(ctx,"preference",[{"message_id":mid,"quote":"以后称呼我小明。"}],conn)
    current=message(db,"查看未完成的请求")
    messages,_=build_messages(db,current)
    assert "小明" not in str(messages) and "F003用途稍后补充" in str(messages)
