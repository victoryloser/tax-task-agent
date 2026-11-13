import json

from tax_agent.domain.finance import Finance
from tax_agent.models import BusinessError
from tax_agent.storage.db import dumps, now, uid

UNFINISHED = ("ACTIVE","WAITING_INPUT","BLOCKED")


def task_record(row):
    result = dict(row)
    for column in ("pending_json","done_json","targets_json","source_refs"):
        result[column.removesuffix("_json")] = json.loads(result.pop(column))
    return result


class Memory:
    def __init__(self, db, authorization=None):
        self.db, self.authorization, self.finance = db, authorization, Finance(db)

    def tasks(self, ctx, include_finished=True, conn=None):
        self.db.scope(ctx,conn=conn)
        if conn is None:
            with self.db.transaction() as connection:
                return self.tasks(ctx,include_finished,connection)
        rows = conn.execute("SELECT * FROM tasks WHERE user_id=? AND enterprise_id=? AND deleted_at IS NULL ORDER BY created_at",(ctx.user_id,ctx.enterprise_id)).fetchall()
        return [task_record(row) for row in rows if include_finished or row["status"] in UNFINISHED]

    def task(self, ctx, task_id, conn):
        row = conn.execute("SELECT * FROM tasks WHERE task_id=? AND user_id=? AND enterprise_id=? AND deleted_at IS NULL",(task_id,ctx.user_id,ctx.enterprise_id)).fetchone()
        if row is None:
            raise BusinessError("TASK_NOT_FOUND", "任务不存在或不属于当前范围", "denied")
        return task_record(row)

    def preferences(self, ctx, conn=None):
        self.db.scope(ctx,conn=conn)
        if conn is None:
            with self.db.transaction() as connection:
                return self.preferences(ctx,connection)
        return [dict(row) | {"source_refs":json.loads(row["source_refs"])} for row in conn.execute("SELECT * FROM preferences WHERE user_id=? AND enterprise_id=? AND deleted_at IS NULL ORDER BY key",(ctx.user_id,ctx.enterprise_id))]

    def invalidate(self, ctx, target_id, refs, conn):
        for ref in refs:
            row = conn.execute("SELECT content FROM messages WHERE message_id=? AND user_id=? AND enterprise_id=?",(ref["message_id"],ctx.user_id,ctx.enterprise_id)).fetchone()
            start = row[0].index(ref["quote"])
            conn.execute("INSERT OR IGNORE INTO invalidated_sources VALUES(?,?,?,?,?,?)",(ctx.user_id,ctx.enterprise_id,ref["message_id"],start,start+len(ref["quote"]),target_id))
        conn.execute("UPDATE memory_scopes SET epoch=epoch+1 WHERE user_id=? AND enterprise_id=?",(ctx.user_id,ctx.enterprise_id))
        conn.execute("DELETE FROM context_summaries WHERE session_id IN (SELECT session_id FROM sessions WHERE user_id=? AND enterprise_id=?)",(ctx.user_id,ctx.enterprise_id))

    def refresh(self, ctx, task_id, conn, operation=None):
        task = self.task(ctx,task_id,conn)
        if task["status"] not in UNFINISHED:
            return task
        done = dict(task["done"])
        if operation:
            done[operation["invoice_id"]] = operation["operation_id"]
        if task["kind"] == "purpose_check":
            missing = self.finance.missing(ctx,task["month"],conn)
            pending = {"invoice_ids":[r["invoice_id"] for r in missing["items"]],"rules":[{"rule_id":r["rule_id"],"version":r["version"],"content_hash":r["content_hash"]} for r in missing["rules"]]}
        else:
            pending = {"invoice_ids":[key for key in task["targets"] if key not in done]}
        status = "WAITING_INPUT" if pending["invoice_ids"] else "COMPLETED"
        if task["pending"] == pending and task["done"] == done and task["status"] == status:
            return task
        conn.execute("UPDATE tasks SET pending_json=?,done_json=?,status=?,revision=revision+1 WHERE task_id=?",(dumps(pending),dumps(done),status,task_id))
        return self.task(ctx,task_id,conn)

    def block(self, ctx, reason):
        with self.db.transaction(True) as conn:
            for task in self.tasks(ctx,False,conn):
                pending = task["pending"] | {"error":reason}
                conn.execute("UPDATE tasks SET status='BLOCKED',pending_json=?,revision=revision+1 WHERE task_id=?",(dumps(pending),task["task_id"]))

    def manage_task(self, ctx, args):
        self.db.scope(ctx)
        if args.action in {"list","get"}:
            with self.db.transaction() as conn:
                return {"tasks":[self.task(ctx,args.task_id,conn)] if args.task_id else self.tasks(ctx,True,conn)}
        if args.action in {"create","replace","update"}:
            if not args.month or not args.goal:
                raise BusinessError("TASK_GOAL_REQUIRED","需明确任务月份与目标")
            if args.kind == "purpose_check":
                self.finance.missing(ctx,args.month)
            elif not args.invoice_ids:
                raise BusinessError("TASK_TARGET_REQUIRED","发票补充任务需明确目标票号")
            else:
                for invoice_id in args.invoice_ids:
                    self.finance.get(ctx,invoice_id)
        self.authorization.approve(ctx,"task_"+args.action,args.model_dump(exclude={"source_refs"}),args.source_refs)
        with self.db.transaction(True) as conn:
            self.db.scope(ctx,conn=conn)
            old = self.task(ctx,args.task_id,conn) if args.task_id else None
            if args.action not in {"create"}:
                if old is None or args.expected_revision != old["revision"]:
                    raise BusinessError("TASK_REVISION_CONFLICT","任务版本已变化，请先读取当前任务","conflict")
            refs = [r.model_dump() for r in args.source_refs]
            if args.action in {"cancel","delete","replace","update"}:
                self.invalidate(ctx,old["task_id"],old["source_refs"],conn)
                conn.execute("UPDATE write_requests SET status='invalidated' WHERE task_id=? AND status='accepted'",(old["task_id"],))
            if args.action in {"cancel","delete","replace"}:
                conn.execute("UPDATE tasks SET status=?,revision=revision+1,deleted_at=? WHERE task_id=?",("SUPERSEDED" if args.action=="replace" else "CANCELLED", now() if args.action=="delete" else None, old["task_id"]))
                if args.action != "replace":
                    return self.db.receipt({"task_id":old["task_id"],"status":"deleted" if args.action=="delete" else "CANCELLED"},conn)
            if args.action in {"create","replace"}:
                if self.tasks(ctx,False,conn):
                    raise BusinessError("UNFINISHED_TASK_EXISTS","当前范围已有未完成任务；临时查询不替换任务，明确替换时使用replace","conflict")
                task_id = uid()
                conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,NULL)",
                    (task_id,ctx.user_id,ctx.enterprise_id,args.month,args.goal,args.kind,dumps(args.invoice_ids),"ACTIVE","{}","{}",ctx.message_id,dumps(refs),now()))
            else:
                task_id = old["task_id"]
                if old["status"] not in UNFINISHED:
                    raise BusinessError("TASK_FINISHED","任务已结束；新的目标需创建新任务")
                if args.action == "update":
                    conn.execute("UPDATE tasks SET month=?,goal=?,kind=?,targets_json=?,done_json='{}',source_message_id=?,source_refs=?,status='ACTIVE',revision=revision+1 WHERE task_id=?",(args.month,args.goal,args.kind,dumps(args.invoice_ids),ctx.message_id,dumps(refs),task_id))
                else:
                    merged = list({(r["message_id"],r["quote"]):r for r in old["source_refs"]+refs}.values())
                    conn.execute("UPDATE tasks SET source_refs=? WHERE task_id=?",(dumps(merged),task_id))
            refreshed = self.refresh(ctx,task_id,conn)
            if args.action == "complete" and refreshed["status"] != "COMPLETED":
                raise BusinessError("TASK_STILL_PENDING","当前业务仍有未完成项，不能直接标为完成")
            self.db.event(ctx,"task_changed",refreshed,conn)
            return self.db.receipt({"task":refreshed},conn)

    def manage_preference(self, ctx, args):
        self.db.scope(ctx)
        if args.action == "list":
            return {"preferences":self.preferences(ctx)}
        if args.action in {"create","update"} and (not args.key or not args.value):
            raise BusinessError("PREFERENCE_VALUE_REQUIRED","需明确偏好键和原文值")
        if args.action in {"create","update"} and not any(args.value in r.quote for r in args.source_refs):
            raise BusinessError("PREFERENCE_SOURCE_VALUE","长期偏好值需取自用户原文，不自行推断")
        self.authorization.approve(ctx,"preference_"+args.action,args.model_dump(exclude={"source_refs"}),args.source_refs)
        refs = [r.model_dump() for r in args.source_refs]
        with self.db.transaction(True) as conn:
            self.db.scope(ctx,conn=conn)
            if args.action == "create":
                if conn.execute("SELECT 1 FROM preferences WHERE user_id=? AND enterprise_id=? AND key=? AND deleted_at IS NULL",(ctx.user_id,ctx.enterprise_id,args.key)).fetchone():
                    raise BusinessError("PREFERENCE_EXISTS","该偏好键已存在，请读取后更正","conflict")
                memory_id = uid()
                conn.execute("INSERT INTO preferences VALUES(?,?,?,?,?,?,?,1,NULL)",(memory_id,ctx.user_id,ctx.enterprise_id,args.key,args.value,ctx.message_id,dumps(refs)))
            else:
                old = conn.execute("SELECT * FROM preferences WHERE memory_id=? AND user_id=? AND enterprise_id=? AND deleted_at IS NULL",(args.memory_id,ctx.user_id,ctx.enterprise_id)).fetchone()
                if old is None or old["revision"] != args.expected_revision:
                    raise BusinessError("PREFERENCE_REVISION_CONFLICT","偏好不存在、归属不符或版本已变化","conflict")
                memory_id = old["memory_id"]
                self.invalidate(ctx,memory_id,json.loads(old["source_refs"]),conn)
                if args.action == "delete":
                    conn.execute("UPDATE preferences SET deleted_at=?,revision=revision+1 WHERE memory_id=?",(now(),memory_id))
                else:
                    conn.execute("UPDATE preferences SET key=?,value=?,source_message_id=?,source_refs=?,revision=revision+1 WHERE memory_id=?",(args.key,args.value,ctx.message_id,dumps(refs),memory_id))
            self.db.event(ctx,"preference_changed",{"action":args.action,"memory_id":memory_id,"sources":refs},conn)
            return self.db.receipt({"memory_id":memory_id,"action":args.action,"preferences":self.preferences(ctx,conn)},conn)
