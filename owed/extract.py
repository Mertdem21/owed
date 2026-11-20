"""Turn receipt emails into structured orders.

A deterministic parser reads the fields; Superlinked SIE (GLiNER) independently extracts entities from the same
email and the two readings are cross-checked. No frontier LLM is needed for this step.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .integrations import sie
from .models import Email, LineItem, Order, Transaction
from .policies import load_policies

FIELD = {
    "order": re.compile(r"Order number:\s*([A-Z]{2}-\d+)", re.I),
    "order_date": re.compile(r"Order date:\s*([A-Za-z]+ \d{1,2}, \d{4})", re.I),
    "delivered": re.compile(r"Delivered on:\s*([A-Za-z]+ \d{1,2}, \d{4})", re.I),
    "total": re.compile(r"Order total:\s*\$([\d,]+\.\d{2})", re.I),
    "txn": re.compile(r"Transaction ID:\s*([A-Z]+-\d+)", re.I),
    "charged": re.compile(r"Charged on:\s*([A-Za-z]+ \d{1,2}, \d{4})", re.I),
    "amount": re.compile(r"Amount:\s*\$([\d,]+\.\d{2})", re.I),
}
ITEM = re.compile(
    r"Item:\s*(?P<name>.+?)\n\s*SKU:\s*(?P<sku>[A-Z0-9-]+)\n(?:\s*Sold by:\s*(?P<seller>.+?)\n)?"
    r"\s*Qty:\s*(?P<qty>\d+)\n\s*Price:\s*\$(?P<price>[\d,]+\.\d{2})", re.I)


def _date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%B %d, %Y").date()
    except ValueError:
        return None


def _money(text: str | None) -> float | None:
    return float(text.replace(",", "")) if text else None


def is_receipt(email: Email) -> bool:
    return bool(FIELD["order"].search(email.body))


def merchant_of(email: Email) -> tuple[str, str] | None:
    hay = (email.sender + " " + email.subject + " " + email.body).lower()
    for pid, policy in load_policies().items():
        name = policy.merchant.lower()
        slug = name.replace(" ", "-")
        if name in hay or slug in hay or pid in hay:
            return pid, policy.merchant
    return None


def parse_orders(emails: list[Email]) -> tuple[list[Order], list[dict[str, Any]]]:
    """Returns (orders, per-email extraction report)."""
    orders: dict[str, Order] = {}
    report: list[dict[str, Any]] = []
    for email in emails:
        if not is_receipt(email):
            report.append({"email_id": email.id, "subject": email.subject, "receipt": False})
            continue
        m = merchant_of(email)
        if not m:
            report.append({"email_id": email.id, "subject": email.subject, "receipt": True, "merchant": None})
            continue
        merchant_id, merchant = m
        body = email.body
        order_id = FIELD["order"].search(body).group(1).upper()
        order = orders.setdefault(order_id, Order(merchant_id=merchant_id, merchant=merchant, order_id=order_id))
        order.source_email_ids.append(email.id)
        if d := _date(_grp(FIELD["order_date"], body)):
            order.order_date = d
        if d := _date(_grp(FIELD["delivered"], body)):
            order.delivered = d
        if t := _money(_grp(FIELD["total"], body)):
            order.total = t
        for it in ITEM.finditer(body + "\n"):
            if not any(li.sku == it["sku"] for li in order.items):
                order.items.append(LineItem(sku=it["sku"], name=it["name"].strip(), qty=int(it["qty"]),
                                            price=_money(it["price"]) or 0.0,
                                            seller=(it["seller"] or merchant).strip()))
        if txn := _grp(FIELD["txn"], body):
            if not any(t.id == txn for t in order.transactions):
                order.transactions.append(Transaction(id=txn, amount=_money(_grp(FIELD["amount"], body)) or 0.0,
                                                      date=_date(_grp(FIELD["charged"], body))))

        entry: dict[str, Any] = {"email_id": email.id, "subject": email.subject, "receipt": True,
                                 "merchant": merchant, "order_id": order_id, "parser": "rules"}
        ents = sie.extract(body)
        if ents is not None:
            found = {e["text"].upper() for e in ents if e.get("label") == "order number"}
            entry["sie_entities"] = ents[:8]
            entry["sie_cross_check"] = "match" if order_id in found else ("no order number found" if not found else f"mismatch: {sorted(found)}")
            if "superlinked-sie" not in order.extracted_by:
                order.extracted_by.append("superlinked-sie")
        if "rules" not in order.extracted_by:
            order.extracted_by.insert(0, "rules")
        report.append(entry)
    return list(orders.values()), report


def _grp(rx: re.Pattern, text: str) -> str | None:
    m = rx.search(text)
    return m.group(1) if m else None
