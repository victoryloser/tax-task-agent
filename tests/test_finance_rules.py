from datetime import date

import pytest

from tax_agent.domain.finance import Finance
from tax_agent.domain.rules import Rules, requires_purpose
from tax_agent.models import BusinessError, CheckPurposes, ListInvoices, QueryFinance, RuleDraft
from tax_agent.storage.import_data import initialize, cents
from conftest import message


@pytest.mark.parametrize("enterprise,month,expected",[
    ("C001","2026-08",(904635,572099,1485075)),("C001","2026-09",(962206,718074,1300050)),
    ("C002","2026-08",(1366705,934110,1880025)),("C002","2026-09",(1266724,808724,1370075)),
])
def test_initial_amounts_are_independent_positive_sums(db,enterprise,month,expected):
    ctx=message(db,"查询金额",enterprise=enterprise)
    result=Finance(db).query(ctx,QueryFinance(month=month))
    assert tuple(result["totals"][k]["amount_cents"] for k in ("invoices","expense","income"))==expected
    assert tuple(result["totals"][k]["count"] for k in ("invoices","expense","income"))==(10,8,4)
    assert {r["rule_id"] for r in result["rules"]}=={"R001","R004"}


def test_initialize_does_not_overwrite_business(db,ctx):
    with db.transaction(True) as conn:
        conn.execute("UPDATE invoices SET purpose='已修改' WHERE enterprise_id='C001' AND invoice_id='F002'")
    with pytest.raises(FileExistsError): initialize(db)
    assert Finance(db).get(ctx,"F002")["purpose"]=="已修改"


def test_money_rounding_is_decimal_and_blank_fields_stay_empty(db,ctx):
    assert cents(9622.060000000001)==962206 and cents("0.105")==11
    row=Finance(db).get(ctx,"F002")
    assert row["purpose"] is None and row["note"] is None


def test_empty_success_and_denial_are_distinct(db):
    ctx=message(db,"查询",user="U002")
    result=Finance(db).query(ctx,QueryFinance(month="2026-07"))
    assert all(r["count"]==0 and r["amount_yuan"]=="0.00" for r in result["totals"].values())
    with pytest.raises(BusinessError,match="无权"):
        Finance(db).query(ctx,QueryFinance(month="2026-09",enterprise_id="C002"))


@pytest.mark.parametrize("enterprise,expected",[("C001",["F002","F003","F004","F006","F009"]),("C002",["F002","F005","F006"])])
def test_missing_purposes_rules_and_pagination(db,enterprise,expected):
    ctx=message(db,"检查9月",enterprise=enterprise)
    finance=Finance(db)
    collected,cursor=[],0
    while True:
        result=finance.check(ctx,CheckPurposes(month="2026-09",cursor=cursor,limit=2))
        assert result["total_count"]==len(expected) and result["checked_count"]==10
        collected.extend(r["invoice_id"] for r in result["items"])
        cursor=result["next_cursor"]
        if cursor is None: break
    assert collected==expected
    expected_rules={("R002","2.0")} if enterprise=="C001" else {("R003","1.0")}
    assert {(r["rule_id"],r["version"]) for r in result["rules"]}==expected_rules


def test_threshold_whitespace_note_and_unsupported_month(db):
    ctx=message(db,"检查",enterprise="C002")
    with db.transaction(True) as conn:
        conn.execute("UPDATE invoices SET purpose='   ',note='有备注' WHERE enterprise_id='C002' AND invoice_id='F002'")
    assert "F002" in [r["invoice_id"] for r in Finance(db).missing(ctx,"2026-09")["items"]]
    assert "F004" not in [r["invoice_id"] for r in Finance(db).missing(ctx,"2026-09")["items"]]
    assert not requires_purpose({"kind":"amount_gte_cents","amount_cents":100000},99999)
    assert requires_purpose({"kind":"amount_gte_cents","amount_cents":100000},100000)
    with pytest.raises(BusinessError,match="仅定义"): Finance(db).missing(ctx,"2026-08")


def draft(version="3.0",start="2026-09-16",kind="amount_gte_cents"):
    return RuleDraft(rule_id="R002",version=version,name="甲企业发票用途规则",content="本版本从生效日起满1000元才必须填写用途。",
        condition={"kind":kind,**({"amount_cents":100000} if kind=="amount_gte_cents" else {})},
        enterprise_ids=["C001"],effective_from=start,source="unit-test-admin-reviewed")


def test_rules_draft_review_overlap_and_invoice_date_selection(db,ctx):
    rules=Rules(db)
    record=rules.draft(draft())
    assert {r["version"] for r in rules.search(ctx,"2026-09")["rules"] if r["rule_id"]=="R002"}=={"2.0"}
    with pytest.raises(BusinessError,match="hash"): rules.activate("R002","3.0","wrong")
    with pytest.raises(BusinessError,match="重叠"): rules.activate("R002","3.0",record["content_hash"])
    rules.activate("R002","3.0",record["content_hash"],supersede="2.0")
    result=Finance(db).missing(ctx,"2026-09")
    assert [r["invoice_id"] for r in result["items"]]==["F002","F004","F006"]
    assert {r["version"] for r in result["rules"]}=={"2.0","3.0"}
    rules.disable("R002","3.0")
    with pytest.raises(BusinessError,match="缺少适用"): Finance(db).missing(ctx,"2026-09")


def test_future_rule_and_runtime_conflict(db,ctx):
    rules=Rules(db)
    record=rules.draft(draft(start="2026-10-01"))
    rules.activate("R002","3.0",record["content_hash"],supersede="2.0")
    assert len(Finance(db).missing(ctx,"2026-09")["items"])==5
    with db.transaction(True) as conn:
        conn.execute("UPDATE rule_versions SET status='active' WHERE rule_id='R002' AND version='1.0'")
    with pytest.raises(BusinessError,match="重叠"): Finance(db).missing(ctx,"2026-09")


def test_rules_and_invoice_lists_never_cross_scope(db,ctx):
    rules=Rules(db).search(ctx,"2026-09")
    assert "R003" not in [r["rule_id"] for r in rules["rules"]]
    with pytest.raises(BusinessError,match="切换"): Finance(db).get(ctx,"F002","C002")
    assert Finance(db).list(ctx,ListInvoices(month="2026-09",limit=3))["next_cursor"]==3
