from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BusinessError(ValueError):
    def __init__(self, code: str, message: str, status: str = "invalid"):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    enterprise_id: str
    session_id: str
    turn_id: str
    message_id: str
    epoch: int


class SourceRef(Contract):
    message_id: str
    quote: str = Field(min_length=1)


class MonthArgs(Contract):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    enterprise_id: str | None = None


class QueryFinance(MonthArgs):
    kind: Literal["all", "invoices", "income", "expense"] = "all"


class GetInvoice(Contract):
    invoice_id: str
    enterprise_id: str | None = None


class ListInvoices(MonthArgs):
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class CheckPurposes(ListInvoices):
    pass


class SearchRules(MonthArgs):
    query: str = ""


class RecallHistory(Contract):
    query: str = ""
    message_id: str | None = None
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=10)


class BatchStatus(Contract):
    turn_id: str | None = None
    include_finished: bool = False
    cursor: int = Field(default=0,ge=0)
    limit: int = Field(default=20,ge=1,le=50)


class BatchCancel(Contract):
    turn_id: str
    source_refs: list[SourceRef] = Field(min_length=1,max_length=8)


class InvoicePatch(Contract):
    purpose: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def specified(self):
        if not self.model_fields_set or any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("至少填写用途或备注；清空请明确传空字符串")
        return self


class UpdateInvoice(GetInvoice):
    patch: InvoicePatch
    expected_version: int = Field(ge=0)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=8)
    task_id: str | None = None
    task_revision: int | None = Field(default=None, ge=1)


class InspectOperation(Contract):
    operation_id: str


class TaskManage(Contract):
    action: Literal["list", "get", "create", "resume", "update", "complete", "cancel", "replace", "delete"]
    task_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    goal: str | None = None
    kind: Literal["purpose_check", "invoice_update"] = "purpose_check"
    invoice_ids: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class PreferenceManage(Contract):
    action: Literal["list", "create", "update", "delete"]
    memory_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    key: str | None = None
    value: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class AuthorizationDecision(Contract):
    allowed: bool
    reason: str


class WriteTarget(Contract):
    invoice_id: str | None = None
    purpose: str | None = None
    note: str | None = None
    missing_fields: list[Literal["invoice_id","purpose","note"]] = Field(default_factory=list)
    previous_turn_id: str | None = None
    source_refs: list[SourceRef] = Field(min_length=1)


class RequestManifest(Contract):
    write_requested: bool
    mode: Literal["per_invoice","atomic"] = "per_invoice"
    targets: list[WriteTarget] = Field(default_factory=list)
    reason: str


class HistorySummary(Contract):
    summary: str = Field(min_length=1,max_length=1600)
    sources: list[SourceRef] = Field(min_length=1,max_length=12)


class RuleCondition(Contract):
    kind: Literal["info", "all_invoices", "amount_gte_cents"]
    amount_cents: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def threshold(self):
        if (self.kind == "amount_gte_cents") != (self.amount_cents is not None):
            raise ValueError("仅amount_gte_cents必须提供amount_cents")
        return self


class RuleDraft(Contract):
    rule_id: str = Field(pattern=r"^R\d{3}$")
    version: str
    name: str
    content: str = Field(min_length=1)
    condition: RuleCondition
    enterprise_ids: list[str] = Field(min_length=1)
    effective_from: date
    effective_to: date | None = None
    source: str

    @model_validator(mode="after")
    def dates(self):
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("规则有效期应为左闭右开且结束晚于开始")
        return self
