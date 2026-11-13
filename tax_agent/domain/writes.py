import json
import sqlite3

from tax_agent.domain.finance import Finance
from tax_agent.domain.memory import Memory, UNFINISHED
from tax_agent.models import BusinessError
from tax_agent.storage.db import digest, dumps


class Writes:
    def __init__(self, db, authorization):
        self.db, self.authorization = db, authorization
        self.finance, self.memory = Finance(db), Memory(db)

    def _after_commit(self, operation_id):
        """故障注入测试的提交边界；生产路径不做其他操作。"""

    def update(self, ctx, args):
        self.db.scope(ctx,args.enterprise_id,write=True)
        patch = args.patch.model_dump(exclude_unset=True)
        refs = [r.model_dump() for r in args.source_refs]
        payload = {"enterprise_id":ctx.enterprise_id,"invoice_id":args.invoice_id,"patch":patch,
                   "source_message_ids":sorted({r.message_id for r in args.source_refs})}
        hashed = digest(payload)
        request_id = digest({"turn_id":ctx.turn_id,"invoice_id":args.invoice_id})
        operation_id = digest({"request_id":request_id,"enterprise_id":ctx.enterprise_id,"invoice_id":args.invoice_id})
        with self.db.transaction() as conn:
            old = conn.execute("SELECT * FROM write_requests WHERE request_id=? AND user_id=? AND enterprise_id=?",(request_id,ctx.user_id,ctx.enterprise_id)).fetchone()
            if old:
                if old["payload_hash"] != hashed:
                    raise BusinessError("REQUEST_PAYLOAD_CONFLICT","同一用户指令的已登记请求不能更换内容；新的更正需新一轮真实用户请求","conflict")
                operation = conn.execute("SELECT * FROM operations WHERE operation_id=?",(operation_id,)).fetchone()
                if operation:
                    return self.verify(ctx,operation_id)
                if old["status"] != "accepted":
                    raise BusinessError("REQUEST_INVALIDATED","原请求已经失效，不能继续修改","conflict")
            unresolved = conn.execute("SELECT operation_id FROM operations WHERE user_id=? AND enterprise_id=? AND state IN ('committed','committed_unverified','outcome_unknown')",(ctx.user_id,ctx.enterprise_id)).fetchone()
            if unresolved:
                raise BusinessError("VERIFY_PREVIOUS_OPERATION","有待核实的保存操作，请先使用inspect_operation："+unresolved[0],"outcome_unknown")
        self.finance.get(ctx,args.invoice_id)
        for field, value in patch.items():
            if value and not any(value in r.quote for r in args.source_refs):
                raise BusinessError("PATCH_SOURCE_VALUE",f"{field}必须逐字来自用户消息；不能推断或改写")
        sources = self.authorization.sources(ctx,args.source_refs)
        if not any(args.invoice_id in r["quote"] for r in sources):
            active = self.memory.tasks(ctx,False)
            if len(active)!=1 or active[0]["pending"].get("invoice_ids") != [args.invoice_id]:
                raise BusinessError("INVOICE_TARGET_AMBIGUOUS","来源未明确票号且不止一个待办，请先明确要修改哪张发票")
        self.authorization.approve(ctx,"invoice_update",payload,args.source_refs)
        with self.db.transaction(True) as conn:
            conn.execute("INSERT OR IGNORE INTO write_requests VALUES(?,?,?,?,?,?,?,?,?,?)",(request_id,ctx.user_id,ctx.enterprise_id,ctx.turn_id,dumps(refs),args.task_id,args.task_revision,dumps(patch),hashed,"accepted"))
        committed = False
        try:
            with self.db.transaction(True) as conn:
                self.db.scope(ctx,args.enterprise_id,True,conn)
                request = conn.execute("SELECT * FROM write_requests WHERE request_id=?",(request_id,)).fetchone()
                if request["status"] != "accepted" or request["payload_hash"] != hashed:
                    raise BusinessError("REQUEST_INVALIDATED","请求状态或参数已变化","conflict")
                if args.task_id:
                    task = self.memory.task(ctx,args.task_id,conn)
                    if task["revision"] != args.task_revision or task["status"] not in UNFINISHED:
                        raise BusinessError("TASK_REVISION_CONFLICT","任务已变更、取消或完成，请先重新读取当前任务","conflict")
                before = self.finance.get(ctx,args.invoice_id,conn=conn)
                if before["row_version"] != args.expected_version:
                    raise BusinessError("ROW_VERSION_CONFLICT","发票版本已变化，请重新查询后按当前用户要求处理","conflict")
                changed = any(before[key] != value for key,value in patch.items())
                if changed:
                    assignments = ",".join(key+"=?" for key in patch)
                    cursor = conn.execute(f"UPDATE invoices SET {assignments},row_version=row_version+1 WHERE enterprise_id=? AND invoice_id=? AND row_version=?",(*patch.values(),ctx.enterprise_id,args.invoice_id,args.expected_version))
                    if cursor.rowcount != 1:
                        raise BusinessError("ROW_VERSION_CONFLICT","发票已被其他操作更新，本次未覆盖","conflict")
                after = self.finance.get(ctx,args.invoice_id,conn=conn)
                result = {"operation_id":operation_id,"request_id":request_id,"enterprise_id":ctx.enterprise_id,"invoice_id":args.invoice_id,
                          "patch":patch,"changed":changed,"row_version":after["row_version"]}
                conn.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)",(operation_id,request_id,ctx.user_id,ctx.enterprise_id,args.invoice_id,dumps(before),dumps(after),after["row_version"],"committed",dumps(result)))
                conn.execute("UPDATE write_requests SET status='committed' WHERE request_id=?",(request_id,))
                self.db.event(ctx,"invoice_committed",result|{"before":before,"after":after,"source_refs":refs},conn)
            committed = True
            self._after_commit(operation_id)
        except BusinessError:
            raise
        except sqlite3.Error as error:
            # 不盲目重写：同事务台账是判断提交结果的依据。
            try:
                with self.db.transaction() as conn:
                    found = conn.execute("SELECT 1 FROM operations WHERE operation_id=?",(operation_id,)).fetchone()
            except sqlite3.Error:
                return {"status":"outcome_unknown","operation_id":operation_id,"request_id":request_id,
                        "invoice_id":args.invoice_id,"error_code":type(error).__name__,"message":"提交结果未核实；暂停后续写入，数据库恢复后先核查操作台账"}
            if not found and not committed:
                raise BusinessError("WRITE_NOT_APPLIED","事务未应用，本轮未保存；可按同一真实请求重新处理","failed") from error
        return self.verify(ctx,operation_id)

    def verify(self, ctx, operation_id):
        self.db.scope(ctx,write=True)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM operations WHERE operation_id=? AND user_id=? AND enterprise_id=?",(operation_id,ctx.user_id,ctx.enterprise_id)).fetchone()
            if row is None:
                raise BusinessError("OPERATION_NOT_FOUND","当前范围未找到操作台账；不能据此推断其他请求未提交","outcome_unknown")
            operation = dict(row)
            result = json.loads(operation["result_json"])
        try:
            current = self.finance.get(ctx,operation["invoice_id"])
        except sqlite3.Error as error:
            with self.db.transaction(True) as conn:
                conn.execute("UPDATE operations SET state='committed_unverified' WHERE operation_id=?",(operation_id,))
                self.db.event(ctx,"readback_failed",{"operation_id":operation_id,"type":type(error).__name__},conn)
            return result|{"status":"committed_unverified","message":"提交台账存在，但独立查回失败；尚未核验保存结果，只能重读"}
        matched = current["row_version"] == operation["row_version"] and all(current[key]==value for key,value in result["patch"].items())
        state = "verified" if matched else "superseded"
        result.update(status="ok" if matched else "conflict",state=state,current=current,
                      message="已保存并独立查回核对" if matched and result["changed"] else "当前已经是该值，未重复修改" if matched else "本次提交有台账，但当前值或版本已被后续操作改变；没有重写或回滚")
        with self.db.transaction(True) as conn:
            conn.execute("UPDATE operations SET state=?,result_json=? WHERE operation_id=?",(state,dumps(result),operation_id))
            conn.execute("UPDATE write_requests SET status=? WHERE request_id=?",(state,operation["request_id"]))
            self.db.event(ctx,"invoice_readback",result,conn)
            if matched:
                request = conn.execute("SELECT task_id FROM write_requests WHERE request_id=?",(operation["request_id"],)).fetchone()
                active = self.memory.tasks(ctx,False,conn)
                targets = [t for t in active if t["kind"]=="purpose_check" or t["task_id"]==request[0]]
                for task in targets:
                    try:
                        result["task"] = self.memory.refresh(ctx,task["task_id"],conn,result)
                    except BusinessError as error:
                        conn.execute("UPDATE tasks SET status='BLOCKED',revision=revision+1,pending_json=? WHERE task_id=?",(dumps(task["pending"]|{"error":str(error)}),task["task_id"]))
                        result["task_error"] = str(error)
        return result
