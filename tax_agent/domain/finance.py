from tax_agent.domain.rules import Rules, month_bounds, requires_purpose
from tax_agent.models import BusinessError
from tax_agent.storage.import_data import money


def invoice(row):
    result = dict(row)
    result["amount_yuan"] = money(result["amount_cents"])
    return result


def page(rows, cursor, limit):
    selected = rows[cursor:cursor+limit]
    return {"items":selected,"total_count":len(rows),"returned_count":len(selected),"next_cursor":cursor+len(selected) if cursor+len(selected)<len(rows) else None}


class Finance:
    def __init__(self, db):
        self.db, self.rules = db, Rules(db)

    def query(self, ctx, args):
        self.db.scope(ctx,args.enterprise_id)
        start, end = month_bounds(args.month)
        with self.db.transaction() as conn:
            result = {}
            if args.kind in {"all","invoices"}:
                row = conn.execute("SELECT COUNT(*),COALESCE(SUM(amount_cents),0) FROM invoices WHERE enterprise_id=? AND invoice_date>=? AND invoice_date<?",(ctx.enterprise_id,start,end)).fetchone()
                result["invoices"] = {"count":row[0],"amount_cents":row[1],"amount_yuan":money(row[1])}
            for kind in ("income","expense") if args.kind == "all" else (args.kind,) if args.kind != "invoices" else ():
                row = conn.execute("SELECT COUNT(*),COALESCE(SUM(amount_cents),0) FROM bank_transactions WHERE enterprise_id=? AND transaction_date>=? AND transaction_date<? AND direction=?",(ctx.enterprise_id,start,end,kind)).fetchone()
                result[kind] = {"count":row[0],"amount_cents":row[1],"amount_yuan":money(row[1])}
        return {"enterprise_id":ctx.enterprise_id,"month":args.month,"totals":result,
                "rules":[r for r in self.rules.search(ctx,args.month)["rules"] if r["condition"]["kind"]=="info"],"calculation":"发票价税合计、银行收入和支出分别相加，整数分计算，不抵销、不关联付款"}

    def get(self, ctx, invoice_id, enterprise_id=None, conn=None):
        self.db.scope(ctx,enterprise_id,conn=conn)
        if conn is None:
            with self.db.transaction() as connection:
                return self.get(ctx,invoice_id,enterprise_id,connection)
        row = conn.execute("SELECT * FROM invoices WHERE enterprise_id=? AND invoice_id=?",(ctx.enterprise_id,invoice_id)).fetchone()
        if row is None:
            raise BusinessError("INVOICE_NOT_FOUND", f"{ctx.enterprise_id}不存在发票{invoice_id}","empty")
        return invoice(row)

    def list(self, ctx, args):
        self.db.scope(ctx,args.enterprise_id)
        start, end = month_bounds(args.month)
        with self.db.transaction() as conn:
            rows = [invoice(r) for r in conn.execute("SELECT * FROM invoices WHERE enterprise_id=? AND invoice_date>=? AND invoice_date<? ORDER BY invoice_id",(ctx.enterprise_id,start,end))]
        return page(rows,args.cursor,args.limit)

    def missing(self, ctx, month, conn=None):
        self.db.scope(ctx,conn=conn)
        if month != "2026-09":
            raise BusinessError("PURPOSE_MONTH_UNSUPPORTED", "材料仅定义2026年9月用途完整性检查；其他月份可查询金额或按明确要求修改")
        if conn is None:
            with self.db.transaction() as connection:
                return self.missing(ctx,month,connection)
        start, end = month_bounds(month)
        rows = conn.execute("SELECT * FROM invoices WHERE enterprise_id=? AND invoice_date>=? AND invoice_date<? ORDER BY invoice_id",(ctx.enterprise_id,start,end)).fetchall()
        missing, used, required_count = [], {}, 0
        for row in rows:
            applicable = [r for r in self.rules.applicable(ctx.enterprise_id,row["invoice_date"],conn) if r["condition"]["kind"] != "info"]
            if not applicable:
                raise BusinessError("RULE_MISSING",f"{row['invoice_date']}缺少适用的发票用途规则，无法给出完整检查结论")
            for rule in applicable:
                used[(rule["rule_id"],rule["version"])] = rule
            if any(requires_purpose(r["condition"],row["amount_cents"]) for r in applicable):
                required_count += 1
                if not (row["purpose"] or "").strip():
                    missing.append(invoice(row))
        return {"items":missing,"total_count":len(missing),"checked_count":len(rows),"required_count":required_count,
                "rules":list(used.values()),"enterprise_id":ctx.enterprise_id,"month":month}

    def check(self, ctx, args):
        self.db.scope(ctx,args.enterprise_id)
        result = self.missing(ctx,args.month)
        return result | page(result["items"],args.cursor,args.limit)
