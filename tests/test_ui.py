from pathlib import Path

from streamlit.testing.v1 import AppTest

from tax_agent.config import ROOT
from tax_agent.ui.app import harness_for
from test_runtime import Scripted, call


def test_streamlit_scope_chat_new_session_and_rerun(db,monkeypatch):
    monkeypatch.setenv("TAX_AGENT_DB",str(db.path))
    provider=Scripted([call("query_finance",{"month":"2026-09"}),{"role":"assistant","content":"9月发票价税合计9622.06元，依据《金额统计说明》1.0版。"}])
    harness_for.clear()
    monkeypatch.setattr("tax_agent.runtime.graph.ChatProvider",lambda:provider)
    app=AppTest.from_file(str(ROOT/"tax_agent/ui/app.py"),default_timeout=30).run()
    assert not app.exception
    assert app.metric[0].value=="9622.06 元"
    app.text_area[0].set_value("查询2026年9月发票总额")
    next(b for b in app.button if b.label=="发送").click().run()
    assert not app.exception
    assert any("9622.06" in m.value for m in app.markdown)
    assert provider.calls==2
    app.run()
    assert provider.calls==2
    first=app.session_state["session_id"]
    next(b for b in app.button if b.label=="新建会话").click().run()
    assert app.session_state["session_id"]!=first and provider.calls==2
    next(v for v in app.selectbox if v.label=="当前用户").select("U002").run()
    assert not app.exception
    enterprise=next(v for v in app.selectbox if v.label=="当前企业")
    assert enterprise.options==["甲企业 · C001"]
    harness_for.clear()


def test_streamlit_background_submit_then_refresh(db,monkeypatch):
    monkeypatch.setenv("TAX_AGENT_DB",str(db.path))
    provider=Scripted([{"role":"assistant","content":"后台已办理"}])
    harness_for.clear()
    monkeypatch.setattr("tax_agent.runtime.graph.ChatProvider",lambda:provider)
    app=AppTest.from_file(str(ROOT/"tax_agent/ui/app.py"),default_timeout=30).run()
    app.checkbox[0].check().run()
    app.text_area[0].set_value("你好")
    next(b for b in app.button if b.label=="发送").click().run()
    assert not app.exception and provider.calls==0
    assert any("排队" in item.value for item in app.info)
    assert harness_for(str(db.path)).run_once().status=="completed"
    next(b for b in app.button if b.label=="刷新办理结果").click().run()
    assert not app.exception
    assert any("后台已办理" in item.value for item in app.markdown)
    assert provider.calls==1
    harness_for.clear()
