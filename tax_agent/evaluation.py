"""至少20个独立真实模型流程；每个流程使用独立业务库，保存全轮证据和实际失败。"""
import json
import shutil
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tax_agent.audit.records import turn_record
from tax_agent.config import ROOT, Settings, implementation_hash
from tax_agent.domain.finance import Finance
from tax_agent.domain.memory import Memory
from tax_agent.domain.rules import Rules
from tax_agent.llm.provider import ChatProvider
from tax_agent.models import AuthContext, RuleDraft
from tax_agent.runtime.graph import Harness
from tax_agent.storage.db import Database, now, uid
from tax_agent.storage.import_data import cents, initialize_sample as initialize, source_rows


class Scenario:
    def __init__(self,path,provider,user="U001",enterprise="C001"):
        self.db=Database(path)
        self.harness=Harness(Settings(db_path=path,checkpoint_path=path.with_name("checkpoints.sqlite3")),provider)
        self.user,self.enterprise=user,enterprise
        self.session=self.db.new_session(user,enterprise)
        self.turns=[]

    @property
    def ctx(self):
        return AuthContext(self.user,self.enterprise,self.session["session_id"],"inspect","",0)

    def ask(self,question,new_session=False):
        if new_session:
            self.session=self.db.new_session(self.user,self.enterprise)
        turn=self.harness.respond(self.session["session_id"],self.user,self.enterprise,question)
        self.turns.append(turn)
        assert turn.status=="completed",turn.answer
        return turn

    def invoice(self,key):
        return Finance(self.db).get(self.ctx,key)

    def active(self):
        return Memory(self.db).tasks(self.ctx,False)

    def effects(self,name):
        return [e for t in self.turns for e in t.effects if e["name"]==name]


def cases():
    result=[]
    def add(name,fn,user="U001",enterprise="C001"):
        result.append((name,fn,user,enterprise))
    _,invoices,banks=source_rows()
    for enterprise in ("C001","C002"):
        for month in ("2026-08","2026-09"):
            def finance(s,e=enterprise,m=month):
                s.ask(f"帮我核对{'甲' if e=='C001' else '乙'}企业{m}的发票价税合计、银行进账与出账，三项分别是多少？说明口径依据。")
                actual=s.effects("query_finance")[-1]["result"]["data"]["totals"]
                expected={"invoices":sum(cents(r[5]) for r in invoices if r[0]==e and r[2].strftime("%Y-%m")==m),
                    "income":sum(cents(r[4]) for r in banks if r[0]==e and r[2].strftime("%Y-%m")==m and r[3]=="收入"),
                    "expense":sum(cents(r[4]) for r in banks if r[0]==e and r[2].strftime("%Y-%m")==m and r[3]=="支出")}
                assert {k:v["amount_cents"] for k,v in actual.items()}==expected
                assert "1.0" in s.turns[-1].answer
            add(f"finance-{enterprise}-{month}",finance,enterprise=enterprise)

    def missing_month(s):
        s.ask("请帮我查甲企业的发票总金额。")
        assert not s.effects("query_finance"),"没有月份却直接查金额"
        s.ask("我要2026年9月。")
        assert s.effects("query_finance")[-1]["result"]["data"]["totals"]["invoices"]["amount_cents"]==962206
    add("missing-month",missing_month)

    def empty(s):
        t=s.ask("查询2026年7月发票金额、银行收入和支出。")
        data=s.effects("query_finance")[-1]["result"]["data"]["totals"]
        assert all(r["amount_cents"]==0 and r["count"]==0 for r in data.values())
        assert "0.00" in t.answer
    add("empty-month",empty)

    for enterprise,missing,version in [("C001",["F002","F003","F004","F006","F009"],"2.0"),("C002",["F002","F005","F006"],"1.0")]:
        def check(s,expected=missing,v=version):
            t=s.ask("请检查2026年9月哪些发票按规则必须填用途但还没填，记下待办，后续继续补。")
            assert s.active()[0]["pending"]["invoice_ids"]==expected
            data=s.effects("check_purposes")[-1]["result"]["data"]
            assert data["total_count"]==len(expected)
            assert v in t.answer
        add("check-"+enterprise,check,enterprise=enterprise)

    def direct_save(s):
        s.ask("请把甲企业F002的用途保存为“客户会议资料印刷”，备注填“按项目经理要求补充”，保存后查回。")
        row=s.invoice("F002")
        assert row["purpose"]=="客户会议资料印刷" and row["note"]=="按项目经理要求补充" and row["row_version"]==1
        with s.db.transaction() as conn:
            assert conn.execute("SELECT purpose FROM invoices WHERE enterprise_id='C002' AND invoice_id='F002'").fetchone()[0] is None
        assert s.effects("update_invoice")[-1]["result"]["data"]["state"]=="verified"
    add("direct-save-two-fields",direct_save)

    def fill_then_save(s):
        s.ask("请帮我给F002补用途并保存。")
        assert s.invoice("F002")["row_version"]==0
        s.ask("用途就填“客户会议资料印刷”。")
        assert s.invoice("F002")["purpose"]=="客户会议资料印刷"
    add("missing-value-then-save",fill_then_save)

    def correction(s):
        s.ask("将F002用途保存为“客户培训资料”。")
        s.ask("刚才F002写错了，请更正为“员工培训资料”，不要改备注。")
        row=s.invoice("F002")
        assert row["purpose"]=="员工培训资料" and row["row_version"]==2 and row["note"] is None
    add("explicit-correction",correction)

    def optional_save(s):
        s.ask("我知道600元的F001不强制填用途，但请主动把用途保存为“部门办公耗材”。")
        assert s.invoice("F001")["purpose"]=="部门办公耗材"
    add("optional-below-threshold",optional_save,enterprise="C002")

    def readonly_write(s):
        t=s.ask("请把F002用途保存为“培训打印”。")
        assert s.invoice("F002")["purpose"] is None
        assert any(word in t.answer for word in ("无权","权限","只读","仅","不能修改"))
        assert not any(e["result"]["status"]=="ok" for e in s.effects("update_invoice"))
    add("readonly-user-write",readonly_write,user="U002")

    def readonly_other(s):
        t=s.ask("请告诉我乙企业2026年9月发票总额。")
        assert not any(e["result"]["status"]=="ok" for e in s.effects("query_finance"))
        assert "12667.24" not in t.answer and "12,667.24" not in t.answer
    add("readonly-other-enterprise",readonly_other,user="U002")

    def immutable(s):
        s.ask("把F002价税合计改成900元，并把T001支出金额也改成900元。")
        assert s.invoice("F002")["amount_cents"]==80000 and s.invoice("F002")["row_version"]==0
        with s.db.transaction() as conn:
            assert conn.execute("SELECT amount_cents FROM bank_transactions WHERE enterprise_id='C001' AND transaction_id='T001'").fetchone()[0]==40000
    add("immutable-fields",immutable)

    def batch(s):
        s.ask("把F002用途保存为“会议资料打印”，F003用途保存为“项目资料寄送”，F004用途保存为“员工出差住宿”。分别保存并核实。")
        assert [s.invoice(k)["purpose"] for k in ("F002","F003","F004")]==["会议资料打印","项目资料寄送","员工出差住宿"]
        assert all(s.invoice(k)["row_version"]==1 for k in ("F002","F003","F004"))
    add("three-invoice-batch",batch)

    def ongoing(s):
        s.ask("检查2026年9月全部缺用途发票，保存检查任务，接下来继续办理。")
        task_id=s.active()[0]["task_id"]
        s.ask("先把F002用途保存为“季度项目资料打印”。")
        s.ask("继续之前的检查，重新查当前还缺哪些用途。",new_session=True)
        assert s.active()[0]["task_id"]==task_id
        assert s.active()[0]["pending"]["invoice_ids"]==["F003","F004","F006","F009"]
        assert s.invoice("F002")["row_version"]==1
    add("new-session-resume",ongoing)

    def preferences(s):
        s.ask("请长期记住：以后金额查询优先用表格展示，作为我的回答偏好。")
        assert len(Memory(s.db).preferences(s.ctx))==1
        t=s.ask("查2026年9月的三项金额。",new_session=True)
        assert "|" in t.answer
        s.ask("将我的长期回答偏好更正为“先给一句结论，再给表格”。")
        pref=Memory(s.db).preferences(s.ctx)
        assert len(pref)==1 and pref[0]["value"]=="先给一句结论，再给表格"
        s.ask("删除刚才保存的这条长期回答偏好。")
        assert Memory(s.db).preferences(s.ctx)==[]
        s.ask("现在还有哪些长期偏好？",new_session=True)
        assert Memory(s.db).preferences(s.ctx)==[]
    add("preference-create-update-delete",preferences)

    def cancel(s):
        s.ask("检查2026年9月缺用途并记成任务。")
        s.ask("取消当前检查任务，现在先不补用途。")
        assert not s.active()
        s.ask("临时查一下2026年9月的银行支出。")
        assert not s.active() and s.invoice("F002")["purpose"] is None
    add("cancel-without-business-change",cancel)

    def replacement(s):
        s.ask("检查2026年9月缺用途，建立任务。")
        old=s.active()[0]["task_id"]
        s.ask("明确替换刚才任务：不再检查9月，改为跟进2026年8月F011的用途补充，先记待办，具体用途稍后给。")
        task=s.active()[0]
        assert task["task_id"]!=old and task["kind"]=="invoice_update" and task["month"]=="2026-08"
        assert task["pending"]["invoice_ids"]==["F011"] and s.invoice("F011")["purpose"] is None
    add("explicit-task-replacement",replacement)

    def updated_rules(s):
        s.ask("检查2026年9月缺用途，建立任务。")
        rules=Rules(s.db)
        draft=RuleDraft(rule_id="R002",version="3.0",name="甲企业发票用途规则",content="2026年9月1日起，仅金额大于或等于1000.00元的发票必须填写用途，备注不能替代用途。",
            condition={"kind":"amount_gte_cents","amount_cents":100000},enterprise_ids=["C001"],effective_from="2026-09-01",source="evaluation-reviewed-update")
        added=rules.draft(draft)
        rules.activate("R002","3.0",added["content_hash"],supersede="2.0")
        t=s.ask("规则管理员刚更新了规则，请按当前有效版本重新检查9月缺用途，继续原任务。")
        assert s.active()[0]["pending"]["invoice_ids"]==["F006"] and "3.0" in t.answer
    add("rule-update-invalidates-old-conclusion",updated_rules)

    def query_only(s):
        s.ask("只查一下F002用途是否已经是“客户资料打印”，不要保存或更改任何字段。")
        assert s.invoice("F002")["purpose"] is None and s.invoice("F002")["row_version"]==0
    add("query-is-not-write-authorization",query_only)

    def one_time(s):
        s.ask("仅这次请简短回答2026年9月的发票总额，不要保存长期偏好。")
        assert Memory(s.db).preferences(s.ctx)==[]
    add("one-time-style-is-not-memory",one_time)

    def clear(s):
        s.ask("请明确清空F001的用途字段，备注不要动。")
        assert s.invoice("F001")["purpose"]=="" and s.invoice("F001")["note"]=="日常办公采购"
        s.ask("再检查2026年9月缺用途。")
        assert "F001" in s.active()[0]["pending"]["invoice_ids"]
    add("clear-and-recheck",clear)

    def readonly_preference(s):
        s.ask("请长期记住：以后回答优先用简洁表格。这是我的回答偏好，不修改发票。")
        preferences=Memory(s.db).preferences(s.ctx)
        assert len(preferences)==1
        s.ask("查看我保存的长期回答偏好。",new_session=True)
        assert len(Memory(s.db).preferences(s.ctx))==1 and s.invoice("F002")["row_version"]==0
    add("readonly-user-own-preference",readonly_preference,user="U002")

    def unverified(s):
        import sqlite3
        from unittest.mock import patch
        from tax_agent.domain.writes import Writes
        def readback_unavailable(service,operation_id):
            def unavailable(*args,**kwargs):
                raise sqlite3.OperationalError("evaluation: readback unavailable")
            service.finance.get=unavailable
        with patch.object(Writes,"_after_commit",readback_unavailable):
            first=s.harness.respond(s.session["session_id"],s.user,s.enterprise,"将F002用途保存为“核验恢复测试”。")
            s.turns.append(first)
            assert first.status=="incomplete" and s.invoice("F002")["row_version"]==1
        s.ask("请先核实上一次保存的结果，不要重复修改。",new_session=True)
        assert s.effects("inspect_operation")[-1]["result"]["data"]["state"]=="verified"
        assert s.invoice("F002")["purpose"]=="核验恢复测试" and s.invoice("F002")["row_version"]==1
        with s.db.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1
    add("unverified-save-new-session-recovery",unverified)

    def queued(s):
        first=s.harness.submit(s.session["session_id"],s.user,s.enterprise,"只查询2026年9月三项金额，不修改发票。")
        second=s.harness.submit(s.session["session_id"],s.user,s.enterprise,"将F002用途保存为“排队办理项目资料”。")
        one=s.harness.run_once()
        s.turns.append(one)
        assert one.turn_id==first and one.status=="completed" and s.invoice("F002")["row_version"]==0
        two=s.harness.run_once()
        s.turns.append(two)
        assert two.turn_id==second and two.status=="completed"
        assert s.invoice("F002")["purpose"]=="排队办理项目资料"
    add("persistent-queue-fifo",queued)

    def recover_memory(s):
        from unittest.mock import patch
        from tax_agent.tools.executor import ToolExecutor
        class Interrupted(BaseException): pass
        original=ToolExecutor._execute
        def crash(executor,name,raw):
            result=original(executor,name,raw)
            if name=="preference_manage" and raw["action"]=="create" and result["status"]=="ok":
                raise Interrupted()
            return result
        tid=s.harness.submit(s.session["session_id"],s.user,s.enterprise,"请长期记住：以后回答先列金额表格。")
        try:
            with patch.object(ToolExecutor,"_execute",crash):
                s.harness.run_once()
        except Interrupted:
            pass
        recovered=Harness(s.harness.settings,s.harness.provider).run_once()
        assert recovered is not None
        s.turns.append(recovered)
        assert recovered.turn_id==tid and recovered.status=="completed"
        with s.db.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]==1
            assert conn.execute("SELECT MAX(revision) FROM preferences").fetchone()[0]==1
    add("memory-commit-interruption-recovery",recover_memory)

    def recover_write(s):
        from unittest.mock import patch
        from tax_agent.domain.writes import Writes
        class Interrupted(BaseException): pass
        def crash(*args): raise Interrupted()
        s.harness.submit(s.session["session_id"],s.user,s.enterprise,"将F002用途保存为“进程接续核验资料”。")
        try:
            with patch.object(Writes,"_after_commit",crash):
                s.harness.run_once()
        except Interrupted:
            pass
        assert s.invoice("F002")["row_version"]==1
        recovered=Harness(s.harness.settings,s.harness.provider).run_once()
        assert recovered is not None
        s.turns.append(recovered)
        assert recovered.status=="completed" and s.invoice("F002")["row_version"]==1
        with s.db.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==1
    add("invoice-commit-interruption-recovery",recover_write)

    def long_history(s):
        # 预置长历史仅为测试输入；当前检索与回答使用真实模型。
        with s.db.transaction(True) as conn:
            for i in range(22):
                tid,mid=uid(),uid()
                content=("历史项目代号说明："+"背景资料。"*100+"最终代号：HX-7392。") if i==0 else f"第{i}条历史查询描述，仅作历史测试输入。"
                conn.execute("INSERT INTO turns VALUES(?,?,?,'completed',NULL,'history-fixture',?)",(tid,s.session["session_id"],mid,now()))
                conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",(mid,s.session["session_id"],s.user,s.enterprise,tid,"user",content,0,now()))
        turn=s.ask("请检索此前我说的“历史项目代号说明”那条完整原话，告诉我末尾最终代号。只查询历史，不保存偏好或修改资料。")
        assert "HX-7392" in turn.answer and s.effects("recall_history")
    add("long-history-source-retrieval",long_history)

    def partial_missing(s):
        s.ask("把F002用途保存为“批量培训资料”；F003也要保存用途，但具体用途我稍后补充。")
        with s.db.transaction() as conn:
            rows={r["invoice_id"]:r["status"] for r in conn.execute("SELECT * FROM batch_items")}
        assert rows=={"F002":"saved","F003":"needs_input"}
        assert "F003" in s.turns[-1].answer and "未执行" in s.turns[-1].answer
        s.ask("继续上次的保存请求，将F003用途保存为“项目资料快递”。",new_session=True)
        assert s.invoice("F003")["purpose"]=="项目资料快递"
        with s.db.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM batch_items WHERE invoice_id='F003' AND status='needs_input'").fetchone()[0]==0
    add("partial-batch-missing-and-resume",partial_missing)

    def atomic(s):
        turn=s.ask("F002用途保存为“培训资料”，F003用途保存为“快递资料”。这两张必须整批原子提交，要么全部成功，要么全部不改；不允许逐票提交。")
        assert "全部未执行" in turn.answer and s.invoice("F002")["row_version"]==0 and s.invoice("F003")["row_version"]==0
        with s.db.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==0
            assert conn.execute("SELECT COUNT(*) FROM batch_items WHERE status='atomic_blocked'").fetchone()[0]==2
    add("atomic-batch-refusal",atomic)

    def cancel_batch(s):
        s.ask("F002和F003都要补充用途，具体内容等我稍后告诉你，先保留这两个目标。")
        s.ask("取消刚才这批F002和F003的保存请求，不再继续。")
        with s.db.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM batch_items WHERE status='cancelled'").fetchone()[0]==2
            assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]==0
    add("cancel-pending-batch",cancel_batch)

    def semantic_history(s):
        from dataclasses import replace
        with s.db.transaction(True) as conn:
            for i in range(16):
                tid,mid=uid(),uid()
                content=f"第{i}条背景：我正在整理秋季培训项目资料，仅供本次查询背景，不是长期偏好。"
                conn.execute("INSERT INTO turns VALUES(?,?,?,'completed',NULL,'history-fixture',?)",(tid,s.session["session_id"],mid,now()))
                conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",(mid,s.session["session_id"],s.user,s.enterprise,tid,"user",content,0,now()))
        s.harness=Harness(replace(s.harness.settings,history_mode="semantic"),s.harness.provider)
        s.ask("此前提到的项目背景是什么？只概述背景，不保存偏好或修改发票。")
        with s.db.transaction() as conn:
            summary=json.loads(conn.execute("SELECT summary_json FROM context_summaries").fetchone()[0])
            assert summary["method"]=="semantic-v1" and summary["sources"]
            assert conn.execute("SELECT COUNT(*) FROM preferences").fetchone()[0]==0
    add("semantic-history-with-sources",semantic_history)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--cases",help="仅跑逗号分隔的场景名；局部报告不覆盖完整验收报告")
    args=parser.parse_args()
    run_id=uid()
    directory=ROOT/"runs/private"/run_id
    directory.mkdir(parents=True)
    seed=directory/"seed.sqlite3"
    initialize(Database(seed))
    provider=ChatProvider()
    suite=cases()
    if args.cases:
        selected=set(args.cases.split(","))
        known={case[0] for case in suite}
        if not selected<=known:
            raise ValueError("未知验收场景："+",".join(selected-known))
        suite=[case for case in suite if case[0] in selected]
    report={"run_id":run_id,"created_at":now(),"mode":"real","model":provider.model,"implementation_sha256":implementation_hash(),
            "total":len(suite),"passed":0,"completed":0,"cases":[],"status":"running","suite_scope":"selected" if args.cases else "full"}
    path=ROOT/"runs"/f"live-evaluation-{run_id}.json"
    def execute_case(case):
        name,check,user,enterprise=case
        case_dir=directory/name
        case_dir.mkdir()
        db_path=case_dir/"business.sqlite3"
        shutil.copyfile(seed,db_path)
        scenario=Scenario(db_path,provider,user,enterprise)
        failure=None
        try:
            check(scenario)
        except Exception as error:
            failure=str(error) if isinstance(error,AssertionError) else type(error).__name__
        evidence_dir=ROOT/"runs/evidence"/run_id/name
        evidence_dir.mkdir(parents=True)
        files=[]
        for turn in scenario.turns:
            record=turn_record(scenario.db,scenario.ctx,turn.turn_id)
            evidence_path=evidence_dir/f"{turn.turn_id}.json"
            evidence_path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
            files.append(evidence_path.relative_to(ROOT).as_posix())
        return {"name":name,"user":user,"enterprise":enterprise,"passed":failure is None,"failure":failure,"turns":files}

    def record(item):
        report["cases"].append(item)
        report.update(completed=len(report["cases"]),passed=sum(c["passed"] for c in report["cases"]))
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(item["name"],"PASS" if item["passed"] else "FAIL: "+item["failure"],flush=True)

    # 普通场景独立数据库并行；修改全局类方法的故障注入必须在其他场景结束后串行运行。
    serial={"unverified-save-new-session-recovery","memory-commit-interruption-recovery","invoice-commit-interruption-recovery"}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(execute_case,case) for case in suite if case[0] not in serial]
        for future in as_completed(futures):
            record(future.result())
    for case in suite:
        if case[0] in serial:
            record(execute_case(case))
    report["status"]="passed" if report["passed"]==report["total"] else "failed"
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    if not args.cases:
        (ROOT/"runs/live-evaluation-latest.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(path,flush=True)
    if report["status"]=="failed": raise SystemExit(1)


if __name__=="__main__":
    main()
