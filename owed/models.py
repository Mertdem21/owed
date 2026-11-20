"""Data model shared by the pipeline, the API and the audit trail."""
from __future__ import annotations

import datetime as _dt
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

OpportunityKind = Literal["price_adjustment", "duplicate_order", "billing_error"]
CaseStatus = Literal["detected", "declined", "needs_approval", "filed", "verified", "rejected", "failed"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Email(BaseModel):
    id: str
    sender: str = ""
    subject: str = ""
    date: str = ""
    body: str = ""


class LineItem(BaseModel):
    sku: str
    name: str = ""
    qty: int = 1
    price: float
    seller: str = ""


class Transaction(BaseModel):
    id: str
    amount: float
    date: Optional[_dt.date] = None


class Order(BaseModel):
    merchant_id: str
    merchant: str
    order_id: str
    order_date: Optional[date] = None
    delivered: Optional[date] = None
    items: list[LineItem] = Field(default_factory=list)
    total: Optional[float] = None
    transactions: list[Transaction] = Field(default_factory=list)
    source_email_ids: list[str] = Field(default_factory=list)
    extracted_by: list[str] = Field(default_factory=list)


class Opportunity(BaseModel):
    id: str
    kind: OpportunityKind
    merchant_id: str
    merchant: str
    order_id: str
    sku: Optional[str] = None
    item_name: str = ""
    amount: float
    facts: dict[str, Any] = Field(default_factory=dict)
    evidence_email_ids: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    provider: str
    source_id: str
    title: str
    text: str
    score: Optional[float] = None
    meta: dict[str, Any] = Field(default_factory=dict)


class Rule(BaseModel):
    kind: OpportunityKind
    window_days: Optional[int] = None
    anchor: str = "purchase_date"
    excludes: list[str] = Field(default_factory=list)
    clause_quote: str = ""
    citation_verified: bool = False
    provider: str = "policy-registry"
    note: str = ""


class Check(BaseModel):
    name: str
    passed: bool
    detail: str


class Decision(BaseModel):
    eligible: bool
    reason: str
    checks: list[Check] = Field(default_factory=list)
    deadline: Optional[date] = None
    days_left: Optional[int] = None


class Case(BaseModel):
    id: str
    opportunity: Opportunity
    status: CaseStatus = "detected"
    decision: Optional[Decision] = None
    rule: Optional[Rule] = None
    evidence: list[Evidence] = Field(default_factory=list)
    claim_id: Optional[str] = None
    refund_id: Optional[str] = None
    amount_recovered: float = 0.0
    message: str = ""
    autonomy: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utcnow)


class AuditEvent(BaseModel):
    run_id: str
    case_id: Optional[str] = None
    ts: datetime = Field(default_factory=utcnow)
    step: str
    provider: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    duration_ms: Optional[int] = None
