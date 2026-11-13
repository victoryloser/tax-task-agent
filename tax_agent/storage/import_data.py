"""仅初始化新库；原始资料保留，已有业务库绝不自动重灌。"""
import hashlib
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from openpyxl import load_workbook

from tax_agent.config import ROOT
from tax_agent.storage.db import Database, dumps


def cents(value):
    return int((Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))*100)


def money(value):
    return f"{Decimal(value)/100:.2f}"


def source_rows(path=None):
    workbook = load_workbook(path or ROOT/"material/02_示例数据.xlsx", data_only=True)
    invoices = [r for r in workbook["发票"].iter_rows(values_only=True) if r[0] in {"C001", "C002"}]
    banks = [r for r in workbook["银行流水"].iter_rows(values_only=True) if r[0] in {"C001", "C002"}]
    return workbook, invoices, banks


def rule_sections(path):
    with ZipFile(path) as archive:
        tree = ET.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    texts = ["".join(t.text or "" for t in p.findall(".//w:t", ns)) for p in tree.findall(".//w:p", ns)]
    result, key = {}, None
    for text in texts:
        match = re.match(r"^(R\d{3}) (.+?) ([\d.]+)版", text)
        if match:
            key = (match[1], match[3])
            result[key] = {"name": match[2], "content": text}
        elif key:
            result[key]["content"] += "\n"+text
    return result


def initialize_sample(db: Database):
    """Historical local regressions only; material files are never distributed."""
    if db.exists:
        raise FileExistsError(f"数据库已存在，初始化不会覆盖已保存的数据：{db.path}")
    required = [ROOT/"material/02_示例数据.xlsx", ROOT/"material/03_业务规则.docx"]
    if not all(path.is_file() for path in required):
        raise FileNotFoundError("本地历史样例未提供；正常使用请通过界面导入自己的 Excel。")
    workbook, invoices, banks = source_rows()
    assert len(invoices) == 40 and len(banks) == 48
    db.schema()
    summaries = defaultdict(lambda: {"invoices": 0, "income": 0, "expense": 0})
    with db.transaction(True) as conn:
        entities = list(workbook["企业与用户"].iter_rows(values_only=True))
        conn.executemany("INSERT INTO enterprises VALUES(?,?,?)", [(r[0],r[1],r[2]) for r in entities if r[0] in {"C001","C002"}])
        for row in entities:
            if row[0] not in {"U001","U002"}:
                continue
            conn.execute("INSERT INTO users VALUES(?,?)", row[:2])
            for enterprise, value in zip(("C001","C002"),row[2:]):
                conn.execute("INSERT INTO enterprise_permissions VALUES(?,?,?,?)", (row[0],enterprise,int(value!="无权访问"),int(value=="可查看并修改")))
                conn.execute("INSERT INTO memory_scopes VALUES(?,?,0)",(row[0],enterprise))
        for enterprise, invoice, day, seller, item, amount, purpose, note in invoices:
            conn.execute("INSERT INTO invoices VALUES(?,?,?,?,?,?,?,?,0)", (str(enterprise),str(invoice),day.date().isoformat(),seller,item,cents(amount),purpose,note))
            summaries[(enterprise,day.strftime("%Y-%m"))]["invoices"] += cents(amount)
        for enterprise, tid, day, direction, amount, counterparty, note in banks:
            kind = {"收入":"income","支出":"expense"}[direction]
            conn.execute("INSERT INTO bank_transactions VALUES(?,?,?,?,?,?,?)", (str(enterprise),str(tid),day.date().isoformat(),kind,cents(amount),counterparty,note))
            summaries[(enterprise,day.strftime("%Y-%m"))][kind] += cents(amount)
        for row in workbook["说明"].iter_rows(values_only=True):
            if row[0] in {"C001","C002"} and row[2] == 10:
                actual = summaries[(row[0], row[1].strftime("%Y-%m"))]
                assert (actual["invoices"],actual["expense"],actual["income"]) == tuple(cents(v) for v in row[4:7])
        sections = rule_sections(ROOT/"material/03_业务规则.docx")
        initial = [
            ("R001","1.0",["C001","C002"],"2026-01-01","active",{"kind":"info"}),
            ("R002","1.0",["C001"],"2026-08-01","disabled",{"kind":"amount_gte_cents","amount_cents":100000}),
            ("R002","2.0",["C001"],"2026-09-01","active",{"kind":"all_invoices"}),
            ("R003","1.0",["C002"],"2026-08-01","active",{"kind":"amount_gte_cents","amount_cents":100000}),
            ("R004","1.0",["C001","C002"],"2026-01-01","active",{"kind":"info"}),
        ]
        assert len(sections) == 5
        for rid, version, scopes, start, status, condition in initial:
            rule = sections[(rid,version)]
            hashed = hashlib.sha256(rule["content"].encode()).hexdigest()
            conn.execute("INSERT INTO rule_versions VALUES(?,?,?,?,?,?,?,?,?,1,?,?)", (rid,version,rule["name"],rule["content"],hashed,dumps(condition),start,None,status,"material/03_业务规则.docx v1.1.0",hashed))
            conn.executemany("INSERT INTO rule_scopes VALUES(?,?,?)", [(rid,version,e) for e in scopes])
        manifest = {"material_version":"1.1.0","invoices":40,"bank_transactions":48,
            "sources":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/"material").iterdir() if p.is_file()},
            "summaries":{f"{e}/{m}":v for (e,m),v in summaries.items()}}
        conn.execute("INSERT INTO metadata VALUES('source_manifest',?)", (dumps(manifest),))
    workbook.close()
    return manifest


def initialize(db: Database, source=None):
    """Create an empty database or import a supplied workbook into a new database."""
    if db.exists:
        raise FileExistsError(f"数据库已存在，初始化不会覆盖已保存的数据：{db.path}")
    if source is not None:
        return import_workbook(db, source)
    db.schema()
    return {"invoices": 0, "bank_transactions": 0}


INVOICE_COLUMNS = ("企业编号", "发票编号", "开票日期", "销售方名称", "项目名称（票面）", "价税合计（元）", "用途", "备注")
BANK_COLUMNS = ("企业编号", "流水编号", "交易日期", "收支方向", "金额（元）", "对方名称", "备注")


def _text(value):
    return str(value).strip() if value is not None else ""


def _day(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(_text(value)).isoformat()


def _amount(value):
    result = cents(value)
    if result <= 0:
        raise ValueError("金额必须大于零（单位：元）")
    if result > 2**63 - 1:
        raise ValueError("金额超过数据库整数分范围")
    return result


def _table(sheet, columns):
    rows = list(sheet.iter_rows(values_only=True))
    for index, row in enumerate(rows):
        if tuple(_text(v) for v in row[:len(columns)]) == columns:
            return [(number, values[:len(columns)]) for number, values in enumerate(rows[index+1:], index+2)
                    if any(v is not None and _text(v) for v in values)]
    raise ValueError(f"{sheet.title}：未找到所需表头：{'、'.join(columns)}")


def import_workbook(db: Database, source):
    """Import the documented XLSX layout, only into a business-empty database."""
    payload = source if isinstance(source, bytes) else Path(source).read_bytes()
    workbook = load_workbook(BytesIO(payload), data_only=True)
    try:
        required = {"企业与用户", "发票", "银行流水"}
        if not required.issubset(workbook.sheetnames):
            raise ValueError("Excel 必须包含工作表：企业与用户、发票、银行流水")
        entities = list(workbook["企业与用户"].iter_rows(values_only=True))
        enterprise_rows = _table(workbook["企业与用户"], ("企业编号", "企业名称", "常用简称"))
        enterprises = []
        for _, row in enterprise_rows:
            key = _text(row[0])
            if key.startswith("C") and re.fullmatch(r"C\d+", key):
                if not _text(row[1]) or not _text(row[2]):
                    raise ValueError("企业名称与简称不能为空")
                enterprises.append((key, _text(row[1]), _text(row[2])))
            elif key == "用户编号":
                break
            else:
                raise ValueError("企业编号须为 C 加数字")
        enterprise_ids = [row[0] for row in enterprises]
        if not enterprise_ids or len(set(enterprise_ids)) != len(enterprise_ids):
            raise ValueError("至少提供一家企业，企业编号不得重复")
        users, permissions = [], []
        header = None
        for number, row in enumerate(entities, 1):
            if _text(row[0]) == "用户编号":
                header = [_text(v) for v in row[2:]]
                if header != enterprise_ids:
                    raise ValueError("用户权限表头须依次填写企业编号，与企业列表顺序一致")
                continue
            if header is None or not any(_text(v) for v in row):
                continue
            user = _text(row[0])
            if not re.fullmatch(r"U\d+", user) or not _text(row[1]):
                raise ValueError(f"企业与用户第 {number} 行：用户编号须为 U 加数字，姓名不能为空")
            users.append((user, _text(row[1])))
            for enterprise, value in zip(header, row[2:]):
                value = _text(value)
                if value not in {"无权访问", "仅可查看", "可查看", "可查看并修改"}:
                    raise ValueError(f"企业与用户第 {number} 行：权限须为无权访问、仅可查看、可查看或可查看并修改")
                permissions.append((user, enterprise, int(value != "无权访问"), int(value == "可查看并修改")))
        if not users or len({row[0] for row in users}) != len(users):
            raise ValueError("至少提供一位用户，用户编号不得重复")
        if not any(row[2] for row in permissions):
            raise ValueError("至少有一位用户可以查看一家企业")
        invoices, banks = [], []
        for sheet, columns, target in (("发票", INVOICE_COLUMNS, invoices), ("银行流水", BANK_COLUMNS, banks)):
            seen = set()
            for number, row in _table(workbook[sheet], columns):
                try:
                    enterprise, key = _text(row[0]), _text(row[1])
                    if enterprise not in enterprise_ids or not key or (enterprise, key) in seen:
                        raise ValueError("企业必须存在，业务编号不能为空或在同一企业内重复")
                    seen.add((enterprise, key))
                    if sheet == "发票":
                        _, _, day, seller, item, amount, purpose, note = row
                        if not _text(seller) or not _text(item):
                            raise ValueError("销售方和项目名称不能为空")
                        target.append((enterprise, key, _day(day), _text(seller), _text(item), _amount(amount), _text(purpose) or None, _text(note) or None, 0))
                    else:
                        _, _, day, direction, amount, counterparty, note = row
                        if direction not in {"收入", "支出"} or not _text(counterparty):
                            raise ValueError("收支方向须为收入或支出，对方名称不能为空")
                        target.append((enterprise, key, _day(day), {"收入":"income", "支出":"expense"}[direction], _amount(amount), _text(counterparty), _text(note) or None))
                except (ValueError, ArithmeticError, TypeError) as error:
                    raise ValueError(f"{sheet}第 {number} 行：{error}") from error
    finally:
        workbook.close()
    db.schema()
    with db.transaction(True) as conn:
        # Recheck inside the write transaction: two imports must never merge silently.
        for table in ("enterprises", "users", "invoices", "bank_transactions", "sessions", "rule_versions"):
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise FileExistsError("已有业务或会话数据，不能重复导入；请使用新的数据库。")
        conn.executemany("INSERT INTO enterprises VALUES(?,?,?)", enterprises)
        conn.executemany("INSERT INTO users VALUES(?,?)", users)
        conn.executemany("INSERT INTO enterprise_permissions VALUES(?,?,?,?)", permissions)
        conn.executemany("INSERT INTO memory_scopes VALUES(?,?,0)", [(row[0], row[1]) for row in permissions])
        conn.executemany("INSERT INTO invoices VALUES(?,?,?,?,?,?,?,?,?)", invoices)
        conn.executemany("INSERT INTO bank_transactions VALUES(?,?,?,?,?,?,?)", banks)
        manifest = {"invoices": len(invoices), "bank_transactions": len(banks), "source_sha256": hashlib.sha256(payload).hexdigest()}
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('source_manifest',?)", (dumps(manifest),))
    return manifest
