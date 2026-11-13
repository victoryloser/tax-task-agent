import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tax_agent.config import Settings
from tax_agent.domain.authorization import Authorization
from tax_agent.domain.memory import Memory
from tax_agent.models import BusinessError
from tax_agent.runtime.graph import Harness
from tax_agent.runtime.locking import scope_lock
from tax_agent.storage.db import dumps, uid
from tax_agent.tools.executor import ToolExecutor
from test_runtime import Scripted, call


def harness(db,tmp_path,replies):
    return Harness(Settings(db_path=db.path,checkpoint_path=tmp_path/"cp.sqlite3"),Scripted(replies))


def test_queue_fifo_and_future_messages_not_in_context(db,tmp_path):
    session=db.new_session("U001","C001")
    seen=[]
    def first(messages):
        seen.append(dumps(messages))
        return {"role":"assistant","content":"first done"}
    h=harness(db,tmp_path,[first,{"role":"assistant","content":"second done"}])
    one=h.submit(session["session_id"],"U001","C001","first question")
    two=h.submit(session["session_id"],"U001","C001","FUTURE_SECRET")
    assert h.result(one,"U001","C001").status=="queued"
    assert h.run_once().turn_id==one and "FUTURE_SECRET" not in seen[0]
    assert h.run_once().turn_id==two and h.run_once() is None
    assert h.result(two,"U001","C001").answer=="second done"
    with pytest.raises(BusinessError):
        h.result(one,"U002","C001")
    with pytest.raises(BusinessError):
        h.submit(session["session_id"],"U001","C001","different",one)


def test_busy_scope_does_not_block_other_enterprise(db,tmp_path):
    h=harness(db,tmp_path,[{"role":"assistant","content":"ok"}])
    a=db.new_session("U001","C001")
    b=db.new_session("U001","C002")
    first=h.submit(a["session_id"],"U001","C001","a")
    second=h.submit(b["session_id"],"U001","C002","b")
    with scope_lock(db,"U001","C001"):
        assert h.run_once().turn_id==second
        assert h.result(first,"U001","C001").status=="queued"


@pytest.mark.parametrize("kind",["preference","task"])
def test_memory_commit_before_tool_result_recovers_once(db,tmp_path,monkeypatch,kind):
    monkeypatch.setattr(Authorization,"approve",lambda *a:None)
    def plan(messages):
        core=json.loads(messages[0]["content"].split("可信当前状态：\n")[1])
        refs=[{"message_id":core["current_message_id"],"quote":"长期记住以后用表格展示，检查2026年9月缺用途"}]
        if kind=="preference":
            return call("preference_manage",{"action":"create","key":"format","value":"以后用表格展示","source_refs":refs})
        return call("task_manage",{"action":"create","month":"2026-09","goal":"检查缺用途","source_refs":refs})
    h=harness(db,tmp_path,[plan])
    session=db.new_session("U001","C001")
    tid=h.submit(session["session_id"],"U001","C001","长期记住以后用表格展示，检查2026年9月缺用途")
    original=ToolExecutor._execute
    def crash(self,*args):
        original(self,*args)
        raise KeyboardInterrupt("after memory commit")
    with monkeypatch.context() as m:
        m.setattr(ToolExecutor,"_execute",crash)
        with pytest.raises(KeyboardInterrupt):
            h.run_once()
    recovered=harness(db,tmp_path,[{"role":"assistant","content":"已完成"}]).run_once()
    assert recovered.turn_id==tid and recovered.status=="completed"
    with db.transaction() as conn:
        table="preferences" if kind=="preference" else "tasks"
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]==1
        assert conn.execute("SELECT attempts FROM turn_jobs WHERE turn_id=?",(tid,)).fetchone()[0]==2
        assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE turn_id=? AND event='tool_execution'",(tid,)).fetchone()[0]==1


def test_delete_receipt_survives_epoch_change(db,tmp_path,monkeypatch):
    from conftest import message
    from tax_agent.models import PreferenceManage, SourceRef
    monkeypatch.setattr(Authorization,"approve",lambda *a:None)
    session=db.new_session("U001","C001")
    ctx=message(db,"长期记住表格",session=session)
    memory=Memory(db,Authorization(db,None))
    saved=memory.manage_preference(ctx,PreferenceManage(action="create",key="style",value="表格",source_refs=[SourceRef(message_id=ctx.message_id,quote="长期记住表格")]))
    def plan(messages):
        core=json.loads(messages[0]["content"].split("可信当前状态：\n")[1])
        return call("preference_manage",{"action":"delete","memory_id":saved["memory_id"],"expected_revision":1,
                    "source_refs":[{"message_id":core["current_message_id"],"quote":"删除这个偏好"}]})
    h=harness(db,tmp_path,[plan])
    tid=h.submit(session["session_id"],"U001","C001","删除这个偏好")
    original=ToolExecutor._execute
    def crash(self,*args):
        original(self,*args)
        raise KeyboardInterrupt()
    with monkeypatch.context() as m:
        m.setattr(ToolExecutor,"_execute",crash)
        with pytest.raises(KeyboardInterrupt): h.run_once()
    h2=harness(db,tmp_path,[{"role":"assistant","content":"已删除"}])
    assert h2.run_once().status=="completed"
    with db.transaction() as conn:
        row=conn.execute("SELECT * FROM preferences").fetchone()
        assert row["deleted_at"] and row["revision"]==2
        assert conn.execute("SELECT epoch FROM memory_scopes").fetchone()[0]==1
    assert len(h2.result(tid,"U001","C001").effects)==1


PROCESS_SCRIPT = """
import json,os,sys,time
from pathlib import Path
from tax_agent.config import Settings
from tax_agent.runtime.graph import Harness
from tax_agent.domain.authorization import Authorization
from tax_agent.domain.writes import Writes
from tax_agent.llm.provider import ModelReply
from tax_agent.storage.db import dumps

mode,db,marker=sys.argv[1:]
Authorization.approve=lambda *a: None
class Provider:
    mode='mock'
    model='process-test'
    def generate(self,messages,tools,tool_choice='auto'):
        if tools[0]['function']['name']=='request_manifest':
            context=json.loads(messages[-1]['content'])['context']
            core=json.loads(context[0]['content'].split('可信当前状态：\\n')[1])
            target={'invoice_id':'F002','purpose':'process recovery','note':None,'missing_fields':[],
                    'source_refs':[{'message_id':core['current_message_id'],'quote':'Save F002 purpose process recovery'}]}
            args={'write_requested':mode!='race','mode':'per_invoice','targets':[] if mode=='race' else [target],'reason':'explicit fixture'}
            return ModelReply({'role':'assistant','tool_calls':[{'id':'manifest','type':'function','function':{'name':'request_manifest','arguments':dumps(args)}}]},self.model,{})
        if mode=='race':
            with open(marker,'a') as f: f.write(str(os.getpid())+'\\n')
            time.sleep(.3)
            return ModelReply({'role':'assistant','content':'done'},self.model,{})
        if messages[-1]['role']=='tool':
            return ModelReply({'role':'assistant','content':'saved'},self.model,{})
        core=json.loads(messages[0]['content'].split('可信当前状态：\\n')[1])
        args={'invoice_id':'F002','patch':{'purpose':'process recovery'},'expected_version':0,
              'source_refs':[{'message_id':core['current_message_id'],'quote':'Save F002 purpose process recovery'}]}
        msg={'role':'assistant','tool_calls':[{'id':'persisted-call','type':'function','function':{'name':'update_invoice','arguments':dumps(args)}}]}
        return ModelReply(msg,self.model,{})
if mode=='crash':
    Writes._after_commit=lambda *a: os._exit(73)
if mode=='supervise':
    import tax_agent.runtime.graph as graph
    graph.ChatProvider=Provider
    def crash_once(*args):
        if not Path(marker).exists():
            Path(marker).write_text('crashed')
            os._exit(73)
    Writes._after_commit=crash_once
if __name__=='__main__':
    settings=Settings(db_path=Path(db),checkpoint_path=Path(db).with_name('process-cp.sqlite3'))
    if mode=='supervise':
        from tax_agent.runtime.worker import serve
        serve(settings,workers=1,once=True)
    else:
        h=Harness(settings,Provider())
        while h.run_once(): pass
"""


def launch(tmp_path,db,mode,marker):
    path=tmp_path/"process_worker.py"
    path.write_text(PROCESS_SCRIPT,encoding="utf-8")
    return subprocess.Popen([sys.executable,str(path),mode,str(db.path),str(marker)],
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)


def test_actual_process_death_after_invoice_commit(db,tmp_path):
    h=harness(db,tmp_path,[])
    session=db.new_session("U001","C001")
    tid=h.submit(session["session_id"],"U001","C001","Save F002 purpose process recovery")
    crashed=launch(tmp_path,db,"crash",tmp_path/"calls")
    stdout,stderr=crashed.communicate(timeout=30)
    assert crashed.returncode==73,stderr.decode()
    with db.transaction() as conn:
        assert conn.execute("SELECT row_version FROM invoices WHERE enterprise_id='C001' AND invoice_id='F002'").fetchone()[0]==1
        assert conn.execute("SELECT status FROM turns WHERE turn_id=?",(tid,)).fetchone()[0]=="running"
    resumed=launch(tmp_path,db,"resume",tmp_path/"calls")
    _,stderr=resumed.communicate(timeout=30)
    assert resumed.returncode==0,stderr.decode()
    assert h.result(tid,"U001","C001").status=="completed"
    with db.transaction() as conn:
        assert conn.execute("SELECT row_version FROM invoices WHERE enterprise_id='C001' AND invoice_id='F002'").fetchone()[0]==1
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1
        assert conn.execute("SELECT state FROM operations").fetchone()[0]=="verified"


def test_two_processes_execute_each_turn_once(db,tmp_path):
    h=harness(db,tmp_path,[])
    sessions=[db.new_session("U001",company) for company in ("C001","C002")]
    tids=[h.submit(sessions[i%2]["session_id"],"U001",sessions[i%2]["enterprise_id"],str(i)) for i in range(6)]
    marker=tmp_path/"calls.txt"
    processes=[launch(tmp_path,db,"race",marker) for _ in range(2)]
    for process in processes:
        _,stderr=process.communicate(timeout=45)
        assert process.returncode==0,stderr.decode()
    lines=marker.read_text().splitlines()
    assert len(lines)==6 and len(set(lines))==2
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE status='completed'").fetchone()[0]==6
        assert conn.execute("SELECT MAX(attempts) FROM turn_jobs").fetchone()[0]==1


def test_supervisor_restarts_crashed_worker_and_finishes_queue(db,tmp_path):
    h=harness(db,tmp_path,[])
    session=db.new_session("U001","C001")
    tid=h.submit(session["session_id"],"U001","C001","Save F002 purpose process recovery")
    process=launch(tmp_path,db,"supervise",tmp_path/"crashed.txt")
    stdout,stderr=process.communicate(timeout=45)
    assert process.returncode==0,stderr.decode()
    assert b"starting replacement" in stdout
    assert h.result(tid,"U001","C001").status=="completed"
    with db.transaction() as conn:
        assert conn.execute("SELECT attempts FROM turn_jobs WHERE turn_id=?",(tid,)).fetchone()[0]==2
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1


def test_recovery_keeps_model_budget(db,tmp_path):
    class CrashProvider:
        model="crash-before-response"
        mode="mock"
        def generate(self,*a):
            raise KeyboardInterrupt()
    settings=Settings(db_path=db.path,checkpoint_path=tmp_path/"cp.sqlite3",max_model_calls=1)
    h=Harness(settings,CrashProvider())
    session=db.new_session("U001","C001")
    h.submit(session["session_id"],"U001","C001","查询金额")
    with pytest.raises(KeyboardInterrupt): h.run_once()
    unused=Scripted([])
    result=Harness(settings,unused).run_once()
    assert result.status=="failed" and unused.calls==0 and "上限" in result.answer
