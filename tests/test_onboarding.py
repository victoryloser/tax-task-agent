"""Data-free startup, workbook validation and business isolation regressions."""
from datetime import date
from io import BytesIO

import pytest
from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

from tax_agent.config import ROOT
from tax_agent.domain.finance import Finance
from tax_agent.models import AuthContext, QueryFinance
from tax_agent.storage.db import Database
from tax_agent.storage.import_data import BANK_COLUMNS, INVOICE_COLUMNS, import_workbook, initialize
from tax_agent.ui.app import harness_for


def workbook_bytes(*, enterprise="C900", user="U900", day="2024-02-15", amount="12.345", permission="可查看并修改", duplicate=False):
    """Only tiny, fictional records; never reads local material or business databases."""
    book = Workbook()
    people = book.active
    people.title = "企业与用户"
    for row in [("企业编号", "企业名称", "常用简称"), (enterprise, "测试企业", "测试企业"),
                (), ("用户编号", "用户名称", enterprise), (user, "测试用户", permission)]:
        people.append(row)
    invoices = book.create_sheet("发票")
    invoices.append(INVOICE_COLUMNS)
    row = (enterprise, "TEST-1", day, "虚构销售方", "测试项目", amount, None, None)
    invoices.append(row)
    if duplicate:
        invoices.append(row)
    bank = book.create_sheet("银行流水")
    bank.append(BANK_COLUMNS)
    bank.append((enterprise, "BANK-1", day, "收入", "40.00", "虚构客户", None))
    bank.append((enterprise, "BANK-2", day, "支出", "5.01", "虚构供应商", None))
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def test_empty_init_and_import_roundtrip(tmp_path):
    db = Database(tmp_path/"business.sqlite3")
    assert initialize(db) == {"invoices": 0, "bank_transactions": 0}
    with pytest.raises(FileExistsError):
        initialize(db)
    result = import_workbook(db, workbook_bytes())
    assert result["invoices"] == 1 and result["bank_transactions"] == 2
    session = db.new_session("U900", "C900")
    ctx = AuthContext("U900", "C900", session["session_id"], "test", "", 0)
    totals = Finance(db).query(ctx, QueryFinance(month="2024-02"))["totals"]
    assert [totals[k]["amount_yuan"] for k in ("invoices", "income", "expense")] == ["12.35", "40.00", "5.01"]
    with pytest.raises(FileExistsError):
        import_workbook(db, workbook_bytes(amount="99.00"))
    assert Finance(db).get(ctx, "TEST-1")["amount_yuan"] == "12.35"


@pytest.mark.parametrize("kwargs", [{"day":"2024-02-30"}, {"amount":"0"}, {"amount":"NaN"},
    {"amount":"Infinity"}, {"amount":"9999999999999999999999999"}, {"permission":"administrator"},
    {"duplicate":True}, {"enterprise":"unknown"}, {"user":"unknown"}])
def test_invalid_workbook_leaves_business_empty(tmp_path, kwargs):
    db = Database(tmp_path/"business.sqlite3")
    initialize(db)
    with pytest.raises((ValueError, ArithmeticError)):
        import_workbook(db, workbook_bytes(**kwargs))
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM enterprises").fetchone()[0] == 0
    assert import_workbook(db, workbook_bytes())["invoices"] == 1


def test_missing_sheets_rejected_before_database_creation(tmp_path):
    output = BytesIO()
    Workbook().save(output)
    db = Database(tmp_path/"not-created.sqlite3")
    with pytest.raises(ValueError, match="工作表"):
        initialize(db, output.getvalue())
    assert not db.exists


def test_readonly_permission_preserved(tmp_path):
    db = Database(tmp_path/"business.sqlite3")
    initialize(db, workbook_bytes(permission="仅可查看"))
    assert db.authorize("U900", "C900")["can_write"] == 0
    with pytest.raises(ValueError, match="无权"):
        db.authorize("U900", "C900", write=True)


def test_first_launch_without_data_or_model(tmp_path, monkeypatch):
    monkeypatch.setenv("TAX_AGENT_DB", str(tmp_path/"empty.sqlite3"))
    monkeypatch.setenv("LLM_API_KEY", "")
    harness_for.clear()
    app = AppTest.from_file(str(ROOT/"tax_agent/ui/app.py"), default_timeout=30).run()
    assert not app.exception
    assert any("上传" in item.value for item in app.info)
    assert not app.text_area
    assert next(button for button in app.button if button.label == "导入资料").disabled
    harness_for.clear()


def test_ui_uses_imported_identity_and_month(tmp_path, monkeypatch):
    db = Database(tmp_path/"custom.sqlite3")
    initialize(db, workbook_bytes(day=date(2024, 2, 15)))
    monkeypatch.setenv("TAX_AGENT_DB", str(db.path))
    harness_for.clear()
    app = AppTest.from_file(str(ROOT/"tax_agent/ui/app.py"), default_timeout=30).run()
    assert not app.exception
    assert next(box for box in app.selectbox if box.label == "当前用户").value == "U900"
    assert next(box for box in app.selectbox if box.label == "资料月份").options == ["2024-02"]
    assert app.metric[0].value == "12.35 元"
    harness_for.clear()


def test_imported_data_runs_tools_and_replays_persisted_result(tmp_path):
    from tax_agent.config import Settings
    from tax_agent.runtime.graph import Harness
    from test_runtime import Scripted, call
    db = Database(tmp_path/"business.sqlite3")
    initialize(db, workbook_bytes())
    session = db.new_session("U900", "C900")
    settings = Settings(db_path=db.path, checkpoint_path=tmp_path/"cp.sqlite3")
    pending = Harness(settings, Scripted([])).submit(session["session_id"], "U900", "C900", "查询2024年2月三项金额")
    provider = Scripted([call("query_finance", {"month":"2024-02"}), {"role":"assistant", "content":"已查清金额"}])
    result = Harness(settings, provider).run_once()
    assert result.turn_id == pending and result.status == "completed"
    assert result.effects[0]["result"]["data"]["totals"]["invoices"]["amount_cents"] == 1235
    replay = Harness(settings, Scripted([])).respond(session["session_id"], "U900", "C900", "查询2024年2月三项金额", pending)
    assert replay.answer == result.answer


@pytest.mark.parametrize("permission,expected_version", [("可查看并修改", 1), ("仅可查看", 0)])
def test_imported_permissions_control_audited_writes(tmp_path, permission, expected_version):
    from tax_agent.config import Settings
    from tax_agent.runtime.graph import Harness
    from test_batches import BatchProvider
    db = Database(tmp_path/"business.sqlite3")
    initialize(db, workbook_bytes(permission=permission))
    session = db.new_session("U900", "C900")
    target = {"invoice_id":"TEST-1", "purpose":"测试培训资料"}
    harness = Harness(Settings(db_path=db.path, checkpoint_path=tmp_path/"cp.sqlite3"), BatchProvider([target], execute=[target]))
    result = harness.respond(session["session_id"], "U900", "C900", "将TEST-1的用途保存为测试培训资料。")
    with db.transaction() as conn:
        row = conn.execute("SELECT purpose,row_version FROM invoices WHERE enterprise_id='C900' AND invoice_id='TEST-1'").fetchone()
        assert row["row_version"] == expected_version
        assert row["purpose"] == ("测试培训资料" if expected_version else None)
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == expected_version
    assert ("已保存" in result.answer) == bool(expected_version)
