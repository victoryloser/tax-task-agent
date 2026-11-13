"""真实Streamlit浏览器演示；独立初始化演示库，默认调用配置模型，录制不超过5分钟。"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from tax_agent.audit.records import turn_record
from tax_agent.config import ROOT, implementation_hash
from tax_agent.models import AuthContext
from tax_agent.storage.db import Database, now, uid
from tax_agent.storage.import_data import initialize_sample as initialize

sys.path.insert(0,str(ROOT/".build/browser-tools"))
from playwright.sync_api import sync_playwright


def main():
    directory=ROOT/".build"/("demo-"+uid()[:10])
    directory.mkdir(parents=True)
    db=Database(directory/"demo.sqlite3")
    initialize(db)
    output=ROOT/".build/browser-demo"
    output.mkdir(exist_ok=True)
    env=os.environ.copy()
    env.update(TAX_AGENT_DB=str(db.path),PYTHONUTF8="1")
    report={"created_at":now(),"implementation_sha256":implementation_hash(),"mode":"real-model-browser","steps":[],"status":"running"}
    report_path=ROOT/"runs/browser-demo.json"
    log=(directory/"streamlit.log").open("w",encoding="utf-8")
    process=subprocess.Popen([sys.executable,"-m","streamlit","run","tax_agent/ui/app.py","--server.port","8503","--server.headless","true"],
        cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    try:
        for _ in range(90):
            try:
                if httpx.get("http://127.0.0.1:8503/_stcore/health",timeout=1).status_code==200: break
            except httpx.ConnectError:
                pass
            time.sleep(.3)
        else: raise RuntimeError("演示服务未启动，见"+str(directory/"streamlit.log"))
        with sync_playwright() as p:
            browser=p.chromium.launch(channel="msedge",headless=True)
            context=browser.new_context(viewport={"width":1440,"height":1000},record_video_dir=str(directory/"video"),record_video_size={"width":1440,"height":1000})
            page=context.new_page()
            start=time.monotonic()
            page.goto("http://127.0.0.1:8503",wait_until="domcontentloaded")
            page.get_by_role("heading",name="把资料查清，把待办办完。").wait_for(timeout=30000)
            page.screenshot(path=str(output/"workspace.png"),full_page=True)

            def step(label):
                report["steps"].append({"label":label,"seconds":round(time.monotonic()-start,2)})
                report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
                print(label,flush=True)

            def ask(question):
                count=page.get_by_test_id("stChatMessage").count()
                page.get_by_label("你的问题",exact=True).fill(question)
                page.get_by_role("button",name="发送",exact=True).click()
                page.wait_for_function("(count)=>document.querySelectorAll('[data-testid=\"stChatMessage\"]').length >= count+2",arg=count,timeout=90000)
                page.get_by_test_id("stChatMessage").last.scroll_into_view_if_needed()
                with db.transaction() as conn:
                    row=conn.execute("SELECT status,answer FROM turns ORDER BY created_at DESC LIMIT 1").fetchone()
                    assert row["status"]=="completed",row["answer"]
                page.wait_for_timeout(800)

            def choose(label,value):
                page.get_by_label(label,exact=True).click()
                page.get_by_role("option",name=value,exact=True).click()
                page.wait_for_timeout(600)

            step("01 月份澄清与实际金额查询")
            ask("请查一下当前企业发票、银行收入和支出总额。")
            ask("2026年9月，简洁列成表格并给出依据。")
            step("02 保存明确长期偏好")
            ask("请长期记住：以后查询金额优先用表格展示。")
            step("03 检查缺用途，逐票保存与更正")
            ask("检查2026年9月哪些发票缺用途，建立待办任务。")
            ask("将F002用途保存为“客户会议资料印刷”，备注保存为“项目资料”，查回核实。")
            ask("更正F002：用途改为“员工培训资料印刷”，备注保持不变。")
            page.get_by_role("tab",name="当前资料",exact=True).click()
            page.screenshot(path=str(output/"saved-invoice.png"),full_page=True)
            page.get_by_role("tab",name="对话办理",exact=True).click()
            step("04 新会话续办，重算待办")
            page.get_by_role("button",name="新建会话",exact=True).click()
            page.wait_for_function("document.querySelectorAll('[data-testid=\"stChatMessage\"]').length===0")
            ask("继续原来的检查，重新列出还缺用途的发票。")
            step("05 更正并删除长期偏好")
            ask("把长期回答偏好更正为“先给一句结论，再给表格”。")
            ask("删除这条长期回答偏好。")
            page.get_by_role("tab",name="任务与偏好",exact=True).click()
            page.screenshot(path=str(output/"task-memory.png"),full_page=True)
            page.get_by_role("tab",name="对话办理",exact=True).click()
            step("06 乙企业使用自己的1000元门槛规则")
            choose("当前企业","乙企业 · C002")
            ask("检查乙企业2026年9月缺用途，保存待办，并说明规则版本。")
            step("07 只读身份拒绝修改")
            choose("模拟用户","U002 · 测试用户二")
            ask("请把F002用途改为“随意更改”。")
            page.get_by_role("tab",name="当前资料",exact=True).click()
            with db.transaction() as conn:
                row=conn.execute("SELECT purpose,note,row_version FROM invoices WHERE enterprise_id='C001' AND invoice_id='F002'").fetchone()
                assert tuple(row)==("员工培训资料印刷","项目资料",2)
                assert conn.execute("SELECT purpose FROM invoices WHERE enterprise_id='C002' AND invoice_id='F002'").fetchone()[0] is None
            step("08 查看实际运行依据")
            page.get_by_role("tab",name="运行依据",exact=True).click()
            page.get_by_text("model_input",exact=False).first.click()
            page.screenshot(path=str(output/"audit.png"),full_page=True)
            step("09 提交后台队列，由两个worker办理后刷新")
            page.get_by_role("tab",name="对话办理",exact=True).click()
            page.get_by_text("后台队列办理",exact=True).click()
            assert page.get_by_label("后台队列办理",exact=True).is_checked()
            page.get_by_label("你的问题",exact=True).fill("查询2026年8月发票、银行收入与支出金额。")
            page.get_by_role("button",name="发送",exact=True).click()
            page.get_by_role("button",name="刷新办理结果",exact=True).wait_for(timeout=30000)
            worker=subprocess.Popen([sys.executable,"-m","tax_agent.cli","--db",str(db.path),"worker","--workers","2","--once"],
                cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            assert worker.wait(timeout=120)==0
            page.get_by_role("button",name="刷新办理结果",exact=True).click()
            page.wait_for_timeout(1000)
            with db.transaction() as conn:
                last=conn.execute("SELECT status,answer FROM turns ORDER BY rowid DESC LIMIT 1").fetchone()
                assert last["status"]=="completed" and "9046.35" in last["answer"].replace(",","")
                events=[json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM audit_events WHERE event='tool_execution' AND turn_id=(SELECT turn_id FROM turns ORDER BY rowid DESC LIMIT 1) ORDER BY rowid")]
                totals={key:value for e in events if e["name"]=="query_finance" and e["result"]["status"]=="ok" for key,value in e["result"]["data"]["totals"].items()}
                assert totals["invoices"]["amount_cents"]==904635 and totals["income"]["amount_cents"]==1485075 and totals["expense"]["amount_cents"]==572099
            page.screenshot(path=str(output/"queue.png"),full_page=True)
            page.wait_for_timeout(1000)
            duration=time.monotonic()-start
            video=page.video
            context.close()
            video_path=Path(video.path())
            shutil.copyfile(video_path,output/"class2-demo.webm")
            mobile=browser.new_context(viewport={"width":375,"height":812},reduced_motion="reduce")
            small=mobile.new_page()
            small.goto("http://127.0.0.1:8503",wait_until="domcontentloaded")
            small.get_by_role("heading",name="把资料查清，把待办办完。").wait_for(timeout=30000)
            assert small.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            small.screenshot(path=str(output/"mobile.png"),full_page=True)
            mobile.close()
            browser.close()
        report.update(status="passed",duration_seconds=round(duration,2),under_five_minutes=duration<=300,
                      video=".build/browser-demo/class2-demo.webm",viewport_checks=["1440x1000","375x812 reduced motion"],database_checks="C001/F002 modified twice and verified; C002/F002 unchanged; U002 modification denied")
        assert duration<=300,"演示超过5分钟"
        with db.transaction() as conn:
            turns=[dict(r) for r in conn.execute("SELECT t.turn_id,s.user_id,s.enterprise_id,s.session_id FROM turns t JOIN sessions s USING(session_id) ORDER BY t.created_at")]
        records=[turn_record(db,AuthContext(r["user_id"],r["enterprise_id"],r["session_id"],r["turn_id"],"",0),r["turn_id"]) for r in turns]
        (output/"demo-evidence.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print("Browser demo passed:",round(duration,2),"seconds",flush=True)
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.close()


if __name__=="__main__":
    main()
