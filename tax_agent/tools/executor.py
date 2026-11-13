import json
import sqlite3

from pydantic import ValidationError

from tax_agent.domain.authorization import Authorization
from tax_agent.domain.finance import Finance
from tax_agent.domain.memory import Memory
from tax_agent.domain.writes import Writes
from tax_agent.models import (BatchCancel, BatchStatus, BusinessError, CheckPurposes, GetInvoice, InspectOperation, ListInvoices,
    PreferenceManage, QueryFinance, RecallHistory, SearchRules, TaskManage, UpdateInvoice)
from tax_agent.storage.db import dumps
from tax_agent.context.history import recall
from tax_agent.domain.batches import Batches
from tax_agent.storage.db import digest

READ_TOOLS={"query_finance","get_invoice","list_invoices","search_rules","check_purposes","recall_history","inspect_operation","batch_status"}

CONTRACTS = {
    "batch_cancel":(BatchCancel,"用户明确取消某轮保存请求时，取消其尚未执行的目标；已保存数据不回滚，待核实操作仍须查回。引用本轮真实用户来源。"),
    "batch_status":(BatchStatus,"分页查看当前用户企业的保存请求清单，包含缺信息、失败、未执行、待核实和原子提交被阻止的目标；按turn_id或状态检索，支持跨会话续办，但旧清单不自动授权新保存。"),
    "recall_history":(RecallHistory,"检索当前会话仍有效的历史用户原话，按关键词子串或message_id定位；分页返回完整消息。摘要不构成新写入授权，当前业务仍须重查。"),
    "query_finance":(QueryFinance,"查询当前企业指定月份发票、银行收入和支出；程序整数分计算并返回规则依据。月份必须明确。"),
    "get_invoice":(GetInvoice,"读取当前发票用途、备注和row_version；保存前读取，不能使用旧记忆代替当前数据。"),
    "list_invoices":(ListInvoices,"分页读取当前企业月份发票；next_cursor非空表示尚未读完整列表。"),
    "search_rules":(SearchRules,"按当前企业、月份、生效期和启用状态检索规则原文、版本与hash。"),
    "check_purposes":(CheckPurposes,"重查2026-09当前发票及逐票适用规则，返回全部缺项总数和分页结果；不自动写发票。"),
    "update_invoice":(UpdateInvoice,"按明确用户要求修改一张发票用途/备注并独立查回。patch仅填要改字段，原文值不改写。引用真实message_id与逐字quote，含本轮补充及必要前序目标。无写权限直接拒绝，不索要用途。多票逐票执行，未知结果先核实。"),
    "inspect_operation":(InspectOperation,"仅查回已登记操作及当前值，核实提交结果；不重写发票。"),
    "task_manage":(TaskManage,"跨会话任务CRUD。检查缺用途后create purpose_check任务；已有任务resume重查。每用户企业最多一个unfinished，临时查询不替换；明确替换才replace。修改需当前revision及真实用户来源。完成必须当前缺项为零；删除取消不回滚发票。"),
    "preference_manage":(PreferenceManage,"用户明确长期要求才create偏好；value逐字来自原文。list查看，update/delete需memory_id和当前revision及真实来源；本次要求不要长期化。"),
}


def schemas():
    return [{"type":"function","function":{"name":name,"description":description,"parameters":model.model_json_schema()}}
            for name,(model,description) in CONTRACTS.items()]


class ToolExecutor:
    def __init__(self, db, ctx, provider, settings=None):
        self.db, self.ctx = db, ctx
        self.provider = provider
        authorization = Authorization(db,provider)
        self.finance = Finance(db)
        self.memory = Memory(db,authorization)
        self.writes = Writes(db,authorization)
        self.write_paused = False
        self.settings = settings

    def execute(self, name, raw, call_id=None):
        if call_id is None:
            return self._execute(name,raw)
        self.db.scope(self.ctx)
        with self.db.transaction(True) as conn:
            row = conn.execute("SELECT * FROM tool_invocations WHERE turn_id=? AND call_id=?",
                               (self.ctx.turn_id,call_id)).fetchone()
            if row:
                if row["name"] != name or row["arguments_json"] != dumps(raw):
                    raise BusinessError("TOOL_CALL_CONFLICT","已登记的工具调用不能更换参数","conflict")
                if row["result_json"]:
                    result = json.loads(row["result_json"])
                    if result["status"] in {"committed_unverified","outcome_unknown"}:
                        self.write_paused = True
                    return result
            else:
                conn.execute("INSERT INTO tool_invocations VALUES(?,?,?,?,NULL,NULL)",
                             (self.ctx.turn_id,call_id,name,dumps(raw)))
        if row and row["data_json"]:
            data = json.loads(row["data_json"])
            result = {"status":data.get("status","ok"),"data":data,"error_code":None,
                      "evidence_ids":[self.db.event(self.ctx,"tool_receipt_recovered",{"call_id":call_id,"name":name})]}
        else:
            self.db.active_call = (self.ctx.turn_id,call_id)
            try:
                result = self._read_budgeted(name,raw)
            finally:
                self.db.active_call = None
        with self.db.transaction(True) as conn:
            conn.execute("UPDATE tool_invocations SET result_json=? WHERE turn_id=? AND call_id=?",
                         (dumps(result),self.ctx.turn_id,call_id))
            self.db.event(self.ctx,"tool_execution",{"name":name,"arguments":raw,"result":result},conn)
        return result

    def _read_budgeted(self,name,raw):
        if self.settings is None or name not in READ_TOOLS:
            return self._execute(name,raw)
        try:
            canonical=CONTRACTS[name][0].model_validate(raw).model_dump()
        except ValidationError:
            return self._execute(name,raw)
        if "enterprise_id" in canonical and canonical["enterprise_id"] is None:
            canonical["enterprise_id"]=self.ctx.enterprise_id
        key=digest({"name":name,"arguments":canonical})
        with self.db.transaction(True) as conn:
            row=conn.execute("SELECT * FROM read_attempts WHERE turn_id=? AND request_key=?",(self.ctx.turn_id,key)).fetchone()
            retry=bool(row and row["last_status"] in {"failed","committed_unverified","outcome_unknown"})
            attempts=row["attempts"] if row else 0
            if retry and attempts>self.settings.read_retry_limit:
                return {"status":"failed","data":{"message":"该只读请求已达到独立重试上限，尚未核实的结果保留"},
                        "error_code":"READ_RETRY_BUDGET","evidence_ids":[self.db.event(self.ctx,"read_retry_exhausted",{"name":name,"request_key":key,"attempts":attempts},conn)]}
            attempts=attempts+1 if retry else 1
            conn.execute("INSERT OR REPLACE INTO read_attempts VALUES(?,?,?,?)",(self.ctx.turn_id,key,attempts,"failed"))
        result=self._execute(name,raw)
        with self.db.transaction(True) as conn:
            conn.execute("UPDATE read_attempts SET last_status=? WHERE turn_id=? AND request_key=?",(result["status"],self.ctx.turn_id,key))
        return result

    def _execute(self, name, raw):
        call_evidence = self.db.event(self.ctx,"tool_call",{"name":name,"arguments":raw})
        try:
            if name not in CONTRACTS:
                raise BusinessError("UNKNOWN_TOOL","没有该工具")
            args = CONTRACTS[name][0].model_validate(raw)
            self.db.scope(self.ctx)
            if name == "batch_cancel":
                data=Batches(self.db,self.ctx).cancel(args,self.provider)
            elif name == "batch_status":
                data=Batches(self.db,self.ctx).list(args)
            elif name == "recall_history":
                data = recall(self.db,self.ctx,args)
            elif name == "query_finance":
                data = self.finance.query(self.ctx,args)
            elif name == "get_invoice":
                data = self.finance.get(self.ctx,args.invoice_id,args.enterprise_id)
            elif name == "list_invoices":
                data = self.finance.list(self.ctx,args)
            elif name == "check_purposes":
                data = self.finance.check(self.ctx,args)
            elif name == "search_rules":
                self.db.scope(self.ctx,args.enterprise_id)
                data = self.finance.rules.search(self.ctx,args.month,args.query)
            elif name == "update_invoice":
                if self.write_paused:
                    raise BusinessError("WRITE_PAUSED","本轮有未核实提交结果，暂停后续修改，请先核实","outcome_unknown")
                if self.settings is not None:
                    Batches(self.db,self.ctx).authorize_target(args)
                data = self.writes.update(self.ctx,args)
            elif name == "inspect_operation":
                data = self.writes.verify(self.ctx,args.operation_id)
                self.write_paused = data["status"] not in {"ok","conflict"}
            elif name == "task_manage":
                data = self.memory.manage_task(self.ctx,args)
            else:
                data = self.memory.manage_preference(self.ctx,args)
            status = data.get("status","empty" if data.get("total_count")==0 else "ok")
            result = {"status":status,"data":data,"error_code":None,"evidence_ids":[call_evidence]}
        except BusinessError as error:
            result = {"status":error.status,"data":{"message":str(error)},"error_code":error.code,"evidence_ids":[call_evidence]}
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            result = {"status":"invalid","data":{"message":str(error)},"error_code":"INVALID_ARGUMENTS","evidence_ids":[call_evidence]}
        except sqlite3.Error as error:
            result = {"status":"outcome_unknown" if name=="update_invoice" else "failed",
                "data":{"message":"数据库操作失败；无法确认的修改需先核实台账","exception":type(error).__name__},
                "error_code":"DATABASE_ERROR","evidence_ids":[call_evidence]}
        if result["status"] in {"committed_unverified","outcome_unknown"}:
            self.write_paused = True
        result["evidence_ids"].append(self.db.event(self.ctx,"tool_result",{"name":name,"result":result}))
        return result
