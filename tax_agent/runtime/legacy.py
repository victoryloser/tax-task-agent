"""核对旧P0中断轮次的已有证据；不猜测和重放未证实的旧写入。"""
import json

from tax_agent.domain.writes import Writes
from tax_agent.models import AuthContext
from tax_agent.runtime.locking import scope_lock
from tax_agent.storage.db import dumps, now, uid


def reconcile(db,apply=False):
    db.schema()
    with db.transaction() as conn:
        candidates=[dict(r) for r in conn.execute("SELECT t.*,s.user_id,s.enterprise_id,s.memory_epoch FROM turns t JOIN sessions s USING(session_id) LEFT JOIN turn_jobs j USING(turn_id) WHERE t.status='running' AND j.turn_id IS NULL ORDER BY t.rowid")]
    reports=[]
    for turn in candidates:
        with scope_lock(db,turn["user_id"],turn["enterprise_id"]):
            ctx=AuthContext(turn["user_id"],turn["enterprise_id"],turn["session_id"],turn["turn_id"],turn["message_id"],turn["memory_epoch"])
            with db.transaction() as conn:
                operations=[dict(r) for r in conn.execute("SELECT * FROM operations o JOIN write_requests w USING(request_id) WHERE w.turn_id=?",(ctx.turn_id,))]
                events=[dict(r) for r in conn.execute("SELECT * FROM audit_events WHERE turn_id=? ORDER BY rowid",(ctx.turn_id,))]
            effects=[json.loads(e["payload_json"]) for e in events if e["event"]=="tool_execution"]
            completed={(e["name"],dumps(e["arguments"])) for e in effects}
            applied_invoices={o["invoice_id"] for o in operations}
            unknown=[]
            for event in events:
                if event["event"]!="tool_call":
                    continue
                item=json.loads(event["payload_json"])
                if item["name"] not in {"update_invoice","task_manage","preference_manage"}:
                    continue
                if (item["name"],dumps(item["arguments"])) in completed:
                    continue
                if item["name"]=="update_invoice" and item["arguments"]["invoice_id"] in applied_invoices:
                    continue
                unknown.append(item)
            report={"turn_id":ctx.turn_id,"operation_ids":[o["operation_id"] for o in operations],
                    "recorded_tool_results":len(effects),"unresolved_calls":unknown,"applied":apply}
            if apply:
                verified=[Writes(db,None).verify(ctx,o["operation_id"]) for o in operations]
                report["readbacks"]=verified
                lines=["旧版本中断记录已核对，未重放任何旧修改。"]
                lines.extend(f"- {r['invoice_id']}：{r['message']}" for r in verified)
                lines.extend(f"- {e['name']}：保留原工具结果 {e['result']['status']}" for e in effects if e["name"]!="update_invoice")
                if unknown:
                    lines.append("以下旧调用缺少可确定结果的回执，保留为待确认；请用新消息明确后续要求：")
                    lines.extend("- "+item["name"]+"："+dumps(item["arguments"]) for item in unknown)
                status="incomplete"
                # 无法从旧日志证明整轮目标全部达成，不将“核对完证据”冒充“原业务全部完成”。
                with db.transaction(True) as conn:
                    conn.execute("UPDATE turns SET status=?,answer=? WHERE turn_id=?",(status,"\n".join(lines),ctx.turn_id))
                    conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",(uid(),ctx.session_id,ctx.user_id,ctx.enterprise_id,ctx.turn_id,"assistant","\n".join(lines),db.epoch(ctx.user_id,ctx.enterprise_id,conn),now()))
                    db.event(ctx,"legacy_reconciled",report,conn)
            reports.append(report)
    return reports
