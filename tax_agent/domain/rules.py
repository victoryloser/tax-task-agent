import hashlib
import json
from datetime import date

from tax_agent.models import BusinessError, RuleCondition, RuleDraft
from tax_agent.storage.db import dumps


def month_bounds(month):
    start = date.fromisoformat(month + "-01")
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start.isoformat(), end.isoformat()


def requires_purpose(condition, amount_cents):
    rule = RuleCondition.model_validate(condition)
    return rule.kind == "all_invoices" or (rule.kind == "amount_gte_cents" and amount_cents >= rule.amount_cents)


def decoded(row):
    value = dict(row)
    value["condition"] = json.loads(value.pop("condition_json"))
    return value


class Rules:
    def __init__(self, db):
        self.db = db

    def applicable(self, enterprise_id, day, conn):
        rows = conn.execute("SELECT r.* FROM rule_versions r JOIN rule_scopes s USING(rule_id,version) WHERE s.enterprise_id=? AND r.status='active' AND r.effective_from<=? AND (r.effective_to IS NULL OR ?<r.effective_to) ORDER BY r.rule_id,r.version", (enterprise_id,day,day)).fetchall()
        ids = [r["rule_id"] for r in rows]
        if len(ids) != len(set(ids)):
            raise BusinessError("RULE_VERSION_CONFLICT", "当前日期有重叠的启用规则版本，请管理员处理后再检查："+dumps([(r["rule_id"],r["version"]) for r in rows]), "conflict")
        return [decoded(r) for r in rows]

    def search(self, ctx, month, query=""):
        self.db.scope(ctx)
        start, end = month_bounds(month)
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT r.* FROM rule_versions r JOIN rule_scopes s USING(rule_id,version) WHERE s.enterprise_id=? AND r.status='active' AND r.effective_from<? AND (r.effective_to IS NULL OR r.effective_to>?) ORDER BY r.rule_id,r.version", (ctx.enterprise_id,end,start)).fetchall()
            days = {start} | {r["effective_from"] for r in rows if start <= r["effective_from"] < end}
            for day in days:
                self.applicable(ctx.enterprise_id, day, conn)
        tokens = {query[i:i+2] for i in range(len(query)-1)}
        results = [decoded(r) for r in rows]
        if query:
            results.sort(key=lambda r: -sum(t in r["name"]+r["content"] for t in tokens))
        return {"rules": results, "month": month, "enterprise_id":ctx.enterprise_id, "total_count":len(results)}

    def list(self):
        with self.db.transaction() as conn:
            results = [decoded(row) for row in conn.execute("SELECT * FROM rule_versions ORDER BY rule_id,version")]
            for rule in results:
                rule["enterprise_ids"] = [r[0] for r in conn.execute("SELECT enterprise_id FROM rule_scopes WHERE rule_id=? AND version=?", (rule["rule_id"],rule["version"]))]
            return results

    def draft(self, draft: RuleDraft):
        hashed = hashlib.sha256(draft.content.encode()).hexdigest()
        with self.db.transaction(True) as conn:
            if conn.execute("SELECT 1 FROM rule_versions WHERE rule_id=? AND version=?",(draft.rule_id,draft.version)).fetchone():
                raise BusinessError("RULE_EXISTS", "已有版本不能覆盖，请创建新版本")
            conn.execute("INSERT INTO rule_versions VALUES(?,?,?,?,?,?,?,?, 'draft',1,?,NULL)",
                (draft.rule_id,draft.version,draft.name,draft.content,hashed,dumps(draft.condition.model_dump(exclude_none=True)),draft.effective_from.isoformat(),draft.effective_to.isoformat() if draft.effective_to else None,draft.source))
            conn.executemany("INSERT INTO rule_scopes VALUES(?,?,?)",[(draft.rule_id,draft.version,e) for e in draft.enterprise_ids])
            self.db.event(None,"rule_drafted",{"draft":draft.model_dump(mode="json"),"content_hash":hashed},conn)
        return {"rule_id":draft.rule_id,"version":draft.version,"status":"draft","content_hash":hashed,
                "review": "核对原文与条件后，以activate --reviewed-hash提交该hash；程序校验不代替语义核对"}

    def activate(self, rule_id, version, reviewed_hash, supersede=None):
        with self.db.transaction(True) as conn:
            row = conn.execute("SELECT * FROM rule_versions WHERE rule_id=? AND version=?",(rule_id,version)).fetchone()
            if row is None or row["status"] != "draft":
                raise BusinessError("RULE_NOT_DRAFT", "只能启用已有草稿")
            if reviewed_hash != row["content_hash"]:
                raise BusinessError("RULE_REVIEW_REQUIRED", "核对原文和执行条件后提供对应content_hash")
            condition = RuleCondition.model_validate_json(row["condition_json"])
            boundary = []
            if condition.kind == "amount_gte_cents":
                threshold = condition.amount_cents
                boundary = [{"amount_cents":threshold-1,"required":False},{"amount_cents":threshold,"required":True},{"amount_cents":threshold+1,"required":True}]
            elif condition.kind == "all_invoices":
                boundary = [{"amount_cents":0,"required":True},{"amount_cents":99999,"required":True}]
            assert all(requires_purpose(condition.model_dump(),case["amount_cents"]) == case["required"] for case in boundary)
            scopes = [r[0] for r in conn.execute("SELECT enterprise_id FROM rule_scopes WHERE rule_id=? AND version=?",(rule_id,version))]
            if supersede:
                old = conn.execute("SELECT * FROM rule_versions WHERE rule_id=? AND version=? AND status='active'",(rule_id,supersede)).fetchone()
                old_scopes = {r[0] for r in conn.execute("SELECT enterprise_id FROM rule_scopes WHERE rule_id=? AND version=?",(rule_id,supersede))}
                if old is None or old["effective_from"] > row["effective_from"] or old_scopes != set(scopes):
                    raise BusinessError("RULE_SUPERSEDE", "替换需指定同一企业范围且起始日期不晚于新版的启用版本")
                if old["effective_from"] == row["effective_from"]:
                    conn.execute("UPDATE rule_versions SET status='disabled',revision=revision+1 WHERE rule_id=? AND version=?",(rule_id,supersede))
                else:
                    conn.execute("UPDATE rule_versions SET effective_to=?,revision=revision+1 WHERE rule_id=? AND version=?",(row["effective_from"],rule_id,supersede))
            for enterprise in scopes:
                conflict = conn.execute("SELECT r.version FROM rule_versions r JOIN rule_scopes s USING(rule_id,version) WHERE r.rule_id=? AND s.enterprise_id=? AND r.status='active' AND r.effective_from<COALESCE(?,'9999-12-31') AND COALESCE(r.effective_to,'9999-12-31')>?",(rule_id,enterprise,row["effective_to"],row["effective_from"])).fetchone()
                if conflict:
                    raise BusinessError("RULE_VERSION_CONFLICT",f"{enterprise}/{rule_id}与启用版本{conflict[0]}的有效期重叠","conflict")
            conn.execute("UPDATE rule_versions SET status='active',reviewed_hash=?,revision=revision+1 WHERE rule_id=? AND version=?",(reviewed_hash,rule_id,version))
            self.db.event(None,"rule_activated",{"rule_id":rule_id,"version":version,"reviewed_hash":reviewed_hash,"boundaries":boundary,"supersede":supersede},conn)
        return {"rule_id":rule_id,"version":version,"status":"active","boundary_checks":boundary}

    def disable(self, rule_id, version):
        with self.db.transaction(True) as conn:
            cursor = conn.execute("UPDATE rule_versions SET status='disabled',revision=revision+1 WHERE rule_id=? AND version=?",(rule_id,version))
            if not cursor.rowcount:
                raise BusinessError("RULE_NOT_FOUND","规则版本不存在")
            self.db.event(None,"rule_disabled",{"rule_id":rule_id,"version":version},conn)
        return {"rule_id":rule_id,"version":version,"status":"disabled"}
