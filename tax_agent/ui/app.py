import os
from pathlib import Path

import pandas as pd
import streamlit as st

from tax_agent.config import Settings
from tax_agent.domain.finance import Finance
from tax_agent.domain.memory import Memory
from tax_agent.domain.batches import Batches
from tax_agent.models import BatchStatus
from tax_agent.models import AuthContext, ListInvoices, QueryFinance
from tax_agent.runtime.graph import Harness
from tax_agent.storage.db import uid
from tax_agent.storage.import_data import import_workbook

STATUS = {"ACTIVE":"进行中","WAITING_INPUT":"等待补充","BLOCKED":"等待处理错误","COMPLETED":"已完成","CANCELLED":"已取消","SUPERSEDED":"已被替换"}


@st.cache_resource
def harness_for(path):
    path = Path(path)
    return Harness(Settings.runtime(path))


def main():
    st.set_page_config(page_title="财税任务助手",page_icon=":material/receipt_long:",layout="wide")
    st.markdown("""<style>
    :root {--ink:#172b40;--muted:#51657a;--line:#dbe5ed;--accent:#0f766e;}
    .block-container {max-width:1280px;padding-top:4rem;padding-bottom:3rem;}
    h1 {letter-spacing:-.025em;} [data-testid="stMetric"] {background:white;border:1px solid var(--line);border-radius:12px;padding:18px;}
    [data-testid="stMetricValue"] {font-variant-numeric:tabular-nums;font-size:1.8rem;}
    [data-testid="stChatMessage"] {background:white;border:1px solid var(--line);border-radius:12px;}
    button {min-height:44px;} .stCaption {color:var(--muted);}
    @media(max-width:600px){.block-container{padding:4rem 1rem 1.2rem;}h1{font-size:1.9rem;}}
    @media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;}}
    </style>""",unsafe_allow_html=True)
    Settings.runtime()  # Load backend settings before selecting the database.
    path = Path(os.getenv("TAX_AGENT_DB",str(Settings().db_path)))
    harness = harness_for(str(path))
    db = harness.db
    with db.transaction() as conn:
        users = [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY user_id")]
    if not users:
        st.title("财税任务助手")
        st.write("接入企业、用户权限、发票和银行流水，开始查询与办理。")
        st.info("请上传包含“企业与用户”“发票”“银行流水”三个工作表的 Excel 文件。")
        with st.expander("查看数据格式"):
            from tax_agent.config import ROOT
            st.markdown((ROOT/"docs/data-format.md").read_text(encoding="utf-8"))
        upload = st.file_uploader("选择企业资料", type=["xlsx"])
        if st.button("导入资料", type="primary", disabled=upload is None):
            try:
                import_workbook(db, upload.getvalue())
            except (ValueError, FileExistsError) as error:
                st.error(str(error))
            except Exception:
                st.error("导入失败。请核对工作表、表头、日期、金额及用户权限，确保符合数据格式说明。")
            else:
                st.rerun()
        st.stop()
    with st.sidebar:
        st.markdown("### 财税任务助手")
        st.caption("企业财税资料与持续任务")
        user_names = {row["user_id"]: row["name"] for row in users}
        user = st.selectbox("当前用户",list(user_names),format_func=lambda v:user_names[v],key="selected_user")
        with db.transaction() as conn:
            enterprises = [dict(r) for r in conn.execute("SELECT e.* FROM enterprises e JOIN enterprise_permissions p USING(enterprise_id) WHERE p.user_id=? AND p.can_read=1 ORDER BY e.enterprise_id",(user,))]
        names = {r["enterprise_id"]:r["alias"] for r in enterprises}
        if not names:
            st.info("当前用户暂无可查看的企业，请联系资料管理员。")
            st.stop()
        enterprise = st.selectbox("当前企业",list(names),format_func=lambda v:f"{names[v]} · {v}",key="selected_enterprise")
        permission = db.authorize(user,enterprise)
        st.caption("权限：可查看并修改用途、备注" if permission["can_write"] else "权限：仅查看")
        queued = st.checkbox("后台办理",value=False,help="适用于已启用后台处理服务的工作台。提交后可离开页面，稍后刷新查看结果。")
        scope = (user,enterprise)
        if st.session_state.get("scope") != scope:
            st.session_state.scope = scope
            st.session_state.session_id = db.new_session(*scope)["session_id"]
            st.session_state.pop("pending_turn",None)
        if st.button("新建会话",use_container_width=True):
            st.session_state.session_id = db.new_session(*scope)["session_id"]
            st.session_state.pop("pending_turn",None)
            st.rerun()
        with db.transaction() as conn:
            sessions = [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE user_id=? AND enterprise_id=? ORDER BY created_at DESC",(user,enterprise))]
        session_labels = {}
        with db.transaction() as conn:
            for item in sessions:
                first = conn.execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY created_at,rowid LIMIT 1", (item["session_id"],)).fetchone()
                session_labels[item["session_id"]] = item["created_at"][:16].replace("T", " ")+" · "+(first[0][:24] if first else "新会话")
        selected = st.selectbox("历史会话",[r["session_id"] for r in sessions],format_func=lambda v:session_labels[v],key="history_choice")
        if st.button("恢复会话",use_container_width=True):
            db.session(selected,*scope)
            st.session_state.session_id = selected
            st.session_state.pop("pending_turn",None)
            st.rerun()
        st.divider()
        st.caption("新会话可继续当前任务。任务与偏好按用户和企业分别保存。")
    session_id = st.session_state.session_id
    session = db.session(session_id,*scope)
    ctx = AuthContext(user,enterprise,session_id,"ui","",session["memory_epoch"])
    st.caption(f"工作台 / {names[enterprise]}")
    st.title("把资料查清，把待办办完。")
    st.write("查询金额、补充发票用途，或继续上一次的检查。每次保存都能查看核验结果。")
    with db.transaction() as conn:
        dates = [row[0] for row in conn.execute("SELECT invoice_date FROM invoices WHERE enterprise_id=? UNION SELECT transaction_date FROM bank_transactions WHERE enterprise_id=?", (enterprise, enterprise))]
    months = sorted({value[:7] for value in dates}, reverse=True)
    if not months:
        st.info("当前企业尚无发票或银行流水，请联系资料管理员补充资料。")
        st.stop()
    month = st.selectbox("资料月份",months,key="view_month")
    totals = Finance(db).query(ctx,QueryFinance(month=month))["totals"]
    columns = st.columns(3)
    for column,(key,label) in zip(columns,[("invoices","发票价税合计"),("income","银行收入"),("expense","银行支出")]):
        column.metric(label,totals[key]["amount_yuan"]+" 元",help=f"所选月份实际记录：{totals[key]['count']}条")
    chat_tab,data_tab,memory_tab,audit_tab = st.tabs(["对话办理","当前资料","任务与偏好","办理记录"])
    with chat_tab:
        active = Memory(db).tasks(ctx,False)
        if active:
            task=active[0]
            st.info(f"当前任务 · {task['month']} · {STATUS[task['status']]}　{task['goal']}\n\n待办："+("、".join(task["pending"].get("invoice_ids",[])) or "等待重新检查"))
        with db.transaction() as conn:
            messages=[dict(row) for row in conn.execute("SELECT role,content,turn_id FROM messages WHERE session_id=? ORDER BY created_at,rowid",(session_id,))]
        if not messages:
            st.markdown("**从一个具体问题开始**")
            st.caption(f"例如：查询{names[enterprise]}{month[:4]}年{int(month[5:])}月的发票金额、银行收入和支出。")
        for message in messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
        with st.form("message_form",clear_on_submit=True):
            question=st.text_area("你的问题",height=90,placeholder="输入查询问题，或提供票号和需要保存的用途、备注。")
            submitted=st.form_submit_button("发送",type="primary")
        if submitted and question.strip():
            st.session_state.pending_turn={"question":question,"turn_id":uid(),"session_id":session_id}
        if "pending_turn" in st.session_state:
            pending=st.session_state.pending_turn
            with st.spinner("正在核对资料与办理结果…"):
                if queued:
                    harness.submit(pending["session_id"],user,enterprise,pending["question"],pending["turn_id"])
                else:
                    harness.respond(pending["session_id"],user,enterprise,pending["question"],pending["turn_id"])
            del st.session_state.pending_turn
            st.rerun()
        with db.transaction() as conn:
            pending_jobs = [dict(r) for r in conn.execute("SELECT turn_id,status FROM turns WHERE session_id=? AND status IN ('queued','running') ORDER BY rowid",(session_id,))]
        if pending_jobs:
            st.info(f"当前会话有{len(pending_jobs)}条排队或办理中的请求，可稍后刷新查看结果。")
            if st.button("刷新办理结果"):
                st.rerun()
    with data_tab:
        st.subheader("当前保存的发票")
        listing=Finance(db).list(ctx,ListInvoices(month=month,limit=50))
        pages = max(1, (listing["total_count"]+49)//50)
        if pages > 1:
            page = st.number_input("发票页码", min_value=1, max_value=pages, step=1, key=f"invoice_page_{user}_{enterprise}_{month}")
            listing=Finance(db).list(ctx,ListInvoices(month=month,limit=50,cursor=(page-1)*50))
        rows=listing["items"]
        frame=pd.DataFrame(rows)
        if rows:
            display=frame[["invoice_id","invoice_date","seller","item","amount_yuan","purpose","note"]].rename(columns={"invoice_id":"票号","invoice_date":"开票日期","seller":"销售方","item":"票面项目","amount_yuan":"价税合计（元）","purpose":"用途","note":"备注"})
            st.dataframe(display,hide_index=True,use_container_width=True)
            st.caption(f"本页 {len(rows)} 条，共 {listing['total_count']} 条。")
            st.download_button("下载本页发票CSV",display.to_csv(index=False).encode("utf-8-sig"),file_name=f"{enterprise}-{month}-invoices.csv",mime="text/csv")
        else:
            st.info("该月份没有发票记录。")
        st.caption("票面项目用于定位发票；实际用途由用户提供。")
    with memory_tab:
        st.subheader("保存请求清单")
        requests=Batches(db,ctx).list(BatchStatus(include_finished=True,limit=50))
        labels={"saved":"已核实","failed":"失败","unverified":"待核实","needs_input":"缺少信息","not_executed":"未执行","atomic_blocked":"整批保存未执行","superseded":"已续办","denied":"无权限","cancelled":"已取消"}
        if requests["items"]:
            st.dataframe(pd.DataFrame([{"票号":r["invoice_id"] or "待明确","状态":labels[r["status"]],"待补充":"、".join(r["target"]["missing_fields"])} for r in requests["items"]]),hide_index=True,use_container_width=True)
            st.caption(f"显示{requests['returned_count']}项，共{requests['total_count']}项。可在对话中要求继续未完成目标或查询完整清单。")
        else:
            st.caption("尚无保存请求。")
        st.subheader("任务进度")
        tasks=Memory(db).tasks(ctx)
        if not tasks:
            st.info("还没有任务。可以在对话中要求检查缺少用途的发票。")
        for task in tasks:
            with st.expander(f"{task['month']} · {STATUS[task['status']]} · {task['goal']}",expanded=task["status"] in {"ACTIVE","WAITING_INPUT","BLOCKED"}):
                st.write("待办："+("、".join(task["pending"].get("invoice_ids",[])) or "无"))
                if task["pending"].get("error"): st.warning(task["pending"]["error"])
        st.subheader("长期偏好")
        preferences=Memory(db).preferences(ctx)
        if preferences:
            st.dataframe(pd.DataFrame(preferences)[["key","value"]].rename(columns={"key":"偏好","value":"用户原文"}),hide_index=True,use_container_width=True)
        else:
            st.caption("暂无长期偏好。只有明确提出的长期要求才会保存。")
        st.caption("在对话中可查看、更正、取消或删除任务和偏好。")
    with audit_tab:
        st.subheader("查询与办理结果")
        with db.transaction() as conn:
            turns=[dict(r) for r in conn.execute("SELECT t.* FROM turns t JOIN sessions s USING(session_id) WHERE s.user_id=? AND s.enterprise_id=? ORDER BY t.created_at DESC",(user,enterprise))]
        if turns:
            state_labels = {"queued":"排队中", "running":"办理中", "completed":"已完成", "failed":"未完成"}
            labels = {}
            with db.transaction() as conn:
                for row in turns:
                    question = conn.execute("SELECT content FROM messages WHERE message_id=?", (row["message_id"],)).fetchone()
                    labels[row["turn_id"]] = row["created_at"][:19].replace("T", " ")+" · "+state_labels.get(row["status"], "未完成")+" · "+(question[0][:30] if question else "办理请求")
            turn_id=st.selectbox("选择办理记录",[r["turn_id"] for r in turns],format_func=lambda value:labels[value])
            turn = next(row for row in turns if row["turn_id"] == turn_id)
            st.markdown(turn["answer"] or "请求正在排队或办理中，请稍后刷新。")
        else:
            st.info("发送第一条消息后，这里可以查看查询与办理结果。")


if __name__=="__main__":
    main()
