import json
from pathlib import Path

import pytest

from tax_agent.audit.records import turn_record
from tax_agent.config import Settings
from tax_agent.context.builder import fit_context
from tax_agent.domain.authorization import Authorization
from tax_agent.llm.provider import ModelReply, RecordedProvider
from tax_agent.models import BusinessError
from tax_agent.runtime.graph import Harness
from tax_agent.storage.db import dumps, uid
from tax_agent.tools.executor import schemas
from conftest import message


class Scripted:
    model="test-scripted"
    mode="mock"
    def __init__(self,replies):
        self.replies=iter(replies)
        self.calls=0
    def generate(self,messages,tools,tool_choice="auto"):
        if tools[0]["function"]["name"]=="request_manifest":
            return ModelReply(call("request_manifest",{"write_requested":False,"mode":"per_invoice","targets":[],"reason":"mock query-only scenario"}),self.model,{})
        self.calls+=1
        reply=next(self.replies)
        if callable(reply): reply=reply(messages)
        return ModelReply(reply,self.model,{"total_tokens":1})


def call(name,args):
    return {"role":"assistant","tool_calls":[{"id":uid(),"type":"function","function":{"name":name,"arguments":dumps(args)}}]}


def test_langgraph_real_tools_checkpoint_rerun_and_audit(db,tmp_path):
    provider=Scripted([call("query_finance",{"month":"2026-09"}),{"role":"assistant","content":"发票9622.06元，收入13000.50元，支出7180.74元。"}])
    settings=Settings(db_path=db.path,checkpoint_path=tmp_path/"checkpoints.sqlite3")
    harness=Harness(settings,provider)
    session=db.new_session("U001","C001")
    turn_id=uid()
    result=harness.respond(session["session_id"],"U001","C001","9月三项金额",turn_id)
    assert result.status=="completed" and result.effects[0]["result"]["data"]["totals"]["invoices"]["amount_cents"]==962206
    repeat=harness.respond(session["session_id"],"U001","C001","9月三项金额",turn_id)
    assert repeat.answer==result.answer and provider.calls==2
    with pytest.raises(BusinessError): harness.respond(session["session_id"],"U001","C001","更换内容",turn_id)
    assert settings.checkpoint_path.exists()
    ctx=message(db,"查看")
    evidence=turn_record(db,ctx,turn_id)
    assert {e["event"] for e in evidence["events"]}>={"model_input","model_output","context_selected","tool_result","turn_completed"}
    assert next(e["payload"]["mode"] for e in evidence["events"] if e["event"]=="model_input")=="mock"


def test_permission_denial_is_visible_to_model_and_no_zero_substitute(db,tmp_path):
    provider=Scripted([call("query_finance",{"month":"2026-09","enterprise_id":"C002"}),{"role":"assistant","content":"没有乙企业访问权限。"}])
    harness=Harness(Settings(db_path=db.path,checkpoint_path=tmp_path/"cp.sqlite3"),provider)
    session=db.new_session("U002","C001")
    result=harness.respond(session["session_id"],"U002","C001","查乙企业金额")
    assert result.effects[0]["result"]["status"]=="denied"
    assert "totals" not in result.effects[0]["result"]["data"]
    with pytest.raises(BusinessError): harness.respond(session["session_id"],"U001","C001","冒用会话")


def test_model_failure_and_budget_stop_without_fallback(db,tmp_path):
    class Failed:
        mode="mock"
        model="failed-provider"
        calls=0
        def generate(self,*args):
            self.calls+=1
            raise TimeoutError("secret_should_not_be_logged")
    provider=Failed()
    harness=Harness(Settings(db_path=db.path,checkpoint_path=tmp_path/"cp.sqlite3"),provider)
    session=db.new_session("U001","C001")
    result=harness.respond(session["session_id"],"U001","C001","查金额")
    assert result.status=="failed" and provider.calls==1 and "secret" not in result.answer
    with db.transaction() as conn:
        assert "secret_should_not_be_logged" not in "".join(r[0] for r in conn.execute("SELECT payload_json FROM audit_events"))
    provider=Scripted([call("get_invoice",{"invoice_id":"F002"})]*4)
    harness=Harness(Settings(db_path=db.path,checkpoint_path=tmp_path/"cp2.sqlite3",max_tool_calls=1),provider)
    result=harness.respond(session["session_id"],"U001","C001","查F002")
    assert result.status=="failed" and len(result.effects)==1 and "上限" in result.answer


def test_trim_keeps_scope_current_message_and_recent_tool_results(db,ctx):
    original=[{"role":"system","content":"scope=C001; month=2026-09; task=remaining F002"},
              {"role":"user","content":"old"*5000},{"role":"assistant","content":"historical"*1000},
              {"role":"user","content":"现在继续"}]
    for i in range(5):
        action=call("get_invoice",{"invoice_id":"F002"})
        original.extend([action,{"role":"tool","tool_call_id":action["tool_calls"][0]["id"],"content":str(i)*2000}])
    trimmed=fit_context(db,ctx,original,[],6000)
    assert trimmed[0]==original[0] and any(m.get("content")=="现在继续" for m in trimmed)
    assert trimmed[-4:]==original[-4:] and len(dumps({"messages":trimmed,"tools":[]}).encode())<6000


def test_auth_semantic_denial_is_not_overridden_by_valid_quote(db,ctx):
    provider=Scripted([call("authorization_decision",{"allowed":False,"reason":"用户仅查询，没有要求保存"})])
    model=RecordedProvider(provider,db,ctx,Settings())
    with pytest.raises(BusinessError,match="没有要求保存"):
        Authorization(db,model).approve(ctx,"invoice_update",{"invoice_id":"F002"},[
            __import__("tax_agent.models",fromlist=["SourceRef"]).SourceRef(message_id=ctx.message_id,quote="请将F002的用途保存为客户培训资料打印")])
