import json
import os
import socket
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver

from tax_agent.config import Settings
from tax_agent.context.builder import build_messages, fit_context
from tax_agent.domain.memory import Memory
from tax_agent.llm.provider import ChatProvider, RecordedProvider
from tax_agent.models import AuthContext, BusinessError
from tax_agent.storage.db import Database, dumps, now, uid
from tax_agent.tools.executor import ToolExecutor, schemas
from tax_agent.runtime.locking import scope_lock
from tax_agent.domain.batches import Batches


class State(TypedDict):
    messages: list[dict]
    effects: list[dict]
    tool_count: int
    epoch: int
    answer: str
    phase: str


@dataclass
class Turn:
    turn_id: str
    answer: str
    status: str
    effects: list[dict]


def mutation_answer(effects):
    writes = [e for e in effects if e["name"] in {"update_invoice","inspect_operation"}]
    if not writes:
        return None
    lines = []
    for effect in writes:
        result, args = effect["result"], effect["arguments"]
        data = result["data"]
        target = data.get("invoice_id",args.get("invoice_id","保存操作"))
        lines.append(f"- {target}：{data.get('message',result['status'])}")
        if result["status"] == "ok":
            for field,value in data["patch"].items():
                lines.append(f"  {'用途' if field=='purpose' else '备注'}：{value if value else '（已清空）'}")
        if "task" in data:
            pending = data["task"]["pending"]["invoice_ids"]
            lines.append("  当前任务剩余："+("、".join(pending) if pending else "无，任务已完成"))
        if data.get("task_error"):
            lines.append("  任务重查未完成："+data["task_error"])
    lines.extend(finance_lines(effects))
    return "\n".join(lines)


def finance_lines(effects):
    lines=[]
    for effect in effects:
        if effect["name"]=="query_finance" and effect["result"]["status"] in {"ok","empty"}:
            data=effect["result"]["data"]
            lines.append(f"{data['enterprise_id']} {data['month']}："+ "；".join(f"{ {'invoices':'发票价税合计','income':'银行收入','expense':'银行支出'}[k]} {v['amount_yuan']}元" for k,v in data["totals"].items()))
    return lines


class Harness:
    def __init__(self, settings: Settings | None = None, provider=None):
        settings = settings or Settings.runtime()
        self.settings, self.db = settings, Database(settings.db_path)
        self.db.schema()
        self.provider = provider

    def respond(self, session_id, user_id, enterprise_id, question, turn_id=None):
        turn_id = self.submit(session_id,user_id,enterprise_id,question,turn_id)
        with scope_lock(self.db,user_id,enterprise_id):
            while self.result(turn_id,user_id,enterprise_id).status in {"queued","running"}:
                job = self._next(user_id,enterprise_id)
                self._run(job)
        return self.result(turn_id,user_id,enterprise_id)

    def submit(self, session_id, user_id, enterprise_id, question, turn_id=None):
        turn_id = turn_id or uid()
        session = self.db.session(session_id,user_id,enterprise_id)
        with self.db.transaction(True) as conn:
            previous = conn.execute("SELECT t.*,m.content FROM turns t JOIN messages m ON m.message_id=t.message_id WHERE t.turn_id=?",(turn_id,)).fetchone()
            if previous:
                if previous["session_id"] != session_id or previous["content"] != question:
                    raise BusinessError("TURN_CONFLICT","同一turn_id不能更换会话或用户消息","conflict")
                if previous["status"] in {"queued","running"}:
                    conn.execute("INSERT OR IGNORE INTO turn_jobs VALUES(?,NULL,0,NULL,?)",(turn_id,now()))
                return turn_id
            message_id = uid()
            conn.execute("INSERT INTO turns VALUES(?,?,?,'queued',NULL,?,?)",(turn_id,session_id,message_id,self.provider.model if self.provider else None,now()))
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",(message_id,session_id,user_id,enterprise_id,turn_id,"user",question,session["memory_epoch"],now()))
            conn.execute("INSERT INTO turn_jobs VALUES(?,NULL,0,NULL,?)",(turn_id,now()))
        return turn_id

    def result(self, turn_id, user_id, enterprise_id):
        self.db.authorize(user_id,enterprise_id)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT t.* FROM turns t JOIN sessions s USING(session_id) WHERE turn_id=? AND s.user_id=? AND s.enterprise_id=?",
                               (turn_id,user_id,enterprise_id)).fetchone()
            if row is None:
                raise BusinessError("TURN_SCOPE","当前范围没有该运行记录","denied")
            effects = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM audit_events WHERE turn_id=? AND event='tool_execution' ORDER BY rowid",(turn_id,))]
        return Turn(turn_id,row["answer"] or "已进入队列，等待办理。",row["status"],effects)

    def _next(self, user_id, enterprise_id):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT t.*,j.state_json,j.attempts,s.user_id,s.enterprise_id FROM turns t JOIN turn_jobs j USING(turn_id) JOIN sessions s USING(session_id) WHERE s.user_id=? AND s.enterprise_id=? AND t.status IN ('queued','running') ORDER BY t.rowid LIMIT 1",
                               (user_id,enterprise_id)).fetchone()
            return dict(row) if row else None

    def run_once(self):
        with self.db.transaction() as conn:
            scopes = conn.execute("SELECT s.user_id,s.enterprise_id FROM turns t JOIN turn_jobs j USING(turn_id) JOIN sessions s USING(session_id) WHERE t.status IN ('queued','running') GROUP BY s.user_id,s.enterprise_id ORDER BY MIN(t.rowid)").fetchall()
        for user,enterprise in scopes:
            with scope_lock(self.db,user,enterprise,blocking=False) as acquired:
                if acquired:
                    job = self._next(user,enterprise)
                    if job:
                        return self._run(job)
        return None

    def _save_state(self, turn_id, state):
        with self.db.transaction(True) as conn:
            conn.execute("UPDATE turn_jobs SET state_json=?,updated_at=? WHERE turn_id=?",(dumps(state),now(),turn_id))

    def _run(self, job):
        turn_id,session_id = job["turn_id"],job["session_id"]
        user_id,enterprise_id = job["user_id"],job["enterprise_id"]
        session = self.db.session(session_id,user_id,enterprise_id)
        message_id = job["message_id"]
        with self.db.transaction(True) as conn:
            conn.execute("UPDATE turns SET status='running' WHERE turn_id=?",(turn_id,))
            conn.execute("UPDATE turn_jobs SET attempts=attempts+1,owner=?,updated_at=? WHERE turn_id=?",(socket.gethostname()+":"+str(os.getpid()),now(),turn_id))
        ctx = AuthContext(user_id,enterprise_id,session_id,turn_id,message_id,session["memory_epoch"])
        saved = json.loads(job["state_json"]) if job["state_json"] else None
        effects = list(saved["effects"]) if saved else []
        status = "completed"
        try:
            provider = self.provider or ChatProvider()
            with self.db.transaction(True) as conn:
                conn.execute("UPDATE turns SET model=? WHERE turn_id=?",(provider.model,turn_id))
            recorded = RecordedProvider(provider,self.db,ctx,self.settings)
            with self.db.transaction() as conn:
                recorded.calls = conn.execute("SELECT COUNT(*) FROM audit_events WHERE turn_id=? AND event='model_input'",(turn_id,)).fetchone()[0]
            batches = Batches(self.db,ctx)
            executor = ToolExecutor(self.db,ctx,recorded,self.settings)
            for effect in effects:
                if effect["result"]["status"] in {"committed_unverified","outcome_unknown"}:
                    executor.write_paused = True
                elif effect["name"]=="inspect_operation" and effect["result"]["status"] in {"ok","conflict"}:
                    executor.write_paused = False
            tools = schemas()
            messages, epoch = build_messages(self.db,ctx,recorded,self.settings.history_mode)
            manifest = batches.prepare(recorded,messages)
            initial = saved or {"messages":messages,"effects":[],"tool_count":0,"epoch":epoch,"answer":"","phase":"model"}
            if manifest["write_requested"] and (manifest["mode"]=="atomic" or not self.db.scope(ctx)["can_write"]):
                initial["phase"]="finish"
            if saved:
                self.db.event(ctx,"turn_recovered",{"attempt":job["attempts"]+1,"phase":saved["phase"],"completed_tools":len(effects)})
                initial["messages"][0] = messages[0]
                # 恢复到模型节点时丢弃已失效记忆；工具节点先按原call_id查事务回执。
                if saved["epoch"] != epoch and saved["phase"] == "model":
                    receipts = [{"name":e["name"],"status":e["result"]["status"]} for e in effects]
                    initial["messages"] = messages + [{"role":"system","content":"本轮已执行记录："+dumps(receipts)+"。已完成操作不要重复执行。"}]
                    initial["epoch"] = epoch

            def model_node(state):
                counter=None
                budget=self.settings.context_bytes
                if self.settings.tokenizer_dir:
                    from tax_agent.context.tokens import count_tokens
                    counter=lambda value:count_tokens(value,tools,self.settings,provider.model)
                    budget=self.settings.context_tokens
                messages = fit_context(self.db,ctx,state["messages"],tools,budget,counter)
                reply = recorded.generate(messages,tools)
                update = {"messages":messages+[reply.message],"phase":"tools" if reply.message.get("tool_calls") else "finish"}
                self._save_state(turn_id,state|update)
                return update

            def tool_node(state):
                nonlocal effects
                messages = list(state["messages"])
                count = state["tool_count"]
                for call in messages[-1]["tool_calls"]:
                    if count >= self.settings.max_tool_calls:
                        raise BusinessError("TOOL_BUDGET","本轮工具调用达到上限，未完成事项已保留","failed")
                    count += 1
                    name = call["function"]["name"]
                    raw = json.loads(call["function"]["arguments"])
                    result = executor.execute(name,raw,call["id"])
                    batches.record(name,raw,result)
                    effect = {"name":name,"arguments":raw,"result":result}
                    effects.append(effect)
                    messages.append({"role":"tool","tool_call_id":call["id"],"content":dumps(result)})
                current, current_epoch = build_messages(self.db,ctx,recorded,self.settings.history_mode)
                if current_epoch != state["epoch"]:
                    # 删除/更正后重新选择权威记忆，不再把旧系统提示或工具历史注入。
                    receipts = [{"name":e["name"],"status":e["result"]["status"],"id":e["result"]["data"].get("memory_id") or e["result"]["data"].get("task_id")} for e in effects]
                    messages = current+[{"role":"system","content":"本轮已执行记录："+dumps(receipts)+"。据此继续或回答，不要重复执行已成功的操作。"}]
                else:
                    messages[0] = current[0]
                update = {"messages":messages,"effects":list(effects),"tool_count":count,"epoch":current_epoch,"phase":"model"}
                self._save_state(turn_id,state|update)
                return update

            def finish(state):
                receipt=batches.answer()
                if receipt:
                    return {"answer":"\n".join([receipt]+finance_lines(state["effects"]))}
                return {"answer":mutation_answer(state["effects"]) or state["messages"][-1].get("content") or "本轮未返回可展示的回答。"}

            graph = StateGraph(State)
            graph.add_node("model",model_node)
            graph.add_node("tools",tool_node)
            graph.add_node("finish",finish)
            graph.add_conditional_edges(START,lambda s:s["phase"])
            graph.add_conditional_edges("model",lambda s:"tools" if s["messages"][-1].get("tool_calls") else "finish")
            graph.add_edge("tools","model")
            graph.add_edge("finish",END)
            self.settings.checkpoint_path.parent.mkdir(parents=True,exist_ok=True)
            with SqliteSaver.from_conn_string(str(self.settings.checkpoint_path)) as saver:
                compiled = graph.compile(checkpointer=saver)
                output = compiled.invoke(initial,
                    {"configurable":{"thread_id":session["thread_id"]},"recursion_limit":2*self.settings.max_tool_calls+8})
            answer = output["answer"]
            if any(item["status"] in {"not_executed","failed","unverified"} for item in batches.items()):
                status = "incomplete"
            if any(e["result"]["status"] in {"failed","outcome_unknown","committed_unverified"} for e in effects):
                status = "incomplete"
                Memory(self.db).block(ctx,"本轮存在失败或未核实的工具结果，详见操作记录")
            self.db.event(ctx,"turn_completed",{"answer":answer,"status":status,"model_calls":recorded.calls,"tool_calls":output["tool_count"],"actual_mode":provider.mode})
        except Exception as error:
            status = "failed"
            message = str(error) if isinstance(error,BusinessError) else type(error).__name__
            receipt = Batches(self.db,ctx).answer() or mutation_answer(effects)
            answer = (receipt+"\n\n" if receipt else "")+"本轮未全部完成："+message+"。已核实的保存保留；待办继续保留。"
            Memory(self.db).block(ctx,message)
            self.db.event(ctx,"turn_failed",{"type":type(error).__name__,"reason":message})
        with self.db.transaction(True) as conn:
            conn.execute("UPDATE turns SET status=?,answer=? WHERE turn_id=?",(status,answer,turn_id))
            epoch = self.db.epoch(user_id,enterprise_id,conn)
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",(uid(),session_id,user_id,enterprise_id,turn_id,"assistant",answer,epoch,now()))
            conn.execute("UPDATE turn_jobs SET owner=NULL,updated_at=? WHERE turn_id=?",(now(),turn_id))
        return Turn(turn_id,answer,status,effects)
