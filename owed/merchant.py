"""Mock merchant backend (orders, live prices, claims portal) for the three demo stores.

The merchant validates every claim against ITS OWN rules - independent of the agent - and approves or rejects it.
The agent talks to it like a real store API; FastAPI exposes the same functions under /merchant/...
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import date
from typing import Any

from .config import DATA, RUNTIME, settings
from .policies import load_policies

CLAIMS_FILE = RUNTIME / "claims.json"
_lock = threading.Lock()


def _seed() -> dict[str, Any]:
    return json.loads((DATA / "merchant_seed.json").read_text())


def _claims() -> dict[str, Any]:
    return json.loads(CLAIMS_FILE.read_text()) if CLAIMS_FILE.exists() else {}


def _save(claims: dict[str, Any]) -> None:
    CLAIMS_FILE.write_text(json.dumps(claims, indent=2, default=str))


def reset() -> None:
    with _lock:
        _save({})


def current_price(merchant_id: str, sku: str) -> dict[str, Any] | None:
    item = _seed().get(merchant_id, {}).get("catalog", {}).get(sku)
    return {"sku": sku, **item} if item else None


def get_order(merchant_id: str, order_id: str) -> dict[str, Any] | None:
    order = _seed().get(merchant_id, {}).get("orders", {}).get(order_id)
    return {"order_id": order_id, **order} if order else None


def support_email(merchant_id: str) -> str:
    return _seed().get(merchant_id, {}).get("support_email", "")


def file_claim(merchant_id: str, claim: dict[str, Any]) -> dict[str, Any]:
    """claim = {type, order_id, sku?, amount, message}. Returns the stored claim."""
    with _lock:
        claims = _claims()
        claim_id = f"{merchant_id[:2].upper()}-CLM-{uuid.uuid4().hex[:6].upper()}"
        record = {"claim_id": claim_id, "merchant_id": merchant_id, "status": "received", **claim}
        claims[claim_id] = record
        _save(claims)
        return record


def claim_status(claim_id: str) -> dict[str, Any]:
    """Processes the claim on first lookup (instant 'support desk') and returns its status."""
    with _lock:
        claims = _claims()
        c = claims.get(claim_id)
        if not c:
            return {"claim_id": claim_id, "status": "not_found"}
        if c["status"] == "received":
            ok, reason, refund = _evaluate(c)
            c["status"] = "approved" if ok else "rejected"
            c["reason"] = reason
            if ok:
                c["refund_amount"] = refund
                c["refund_id"] = f"RF-{uuid.uuid4().hex[:8].upper()}"
            _save(claims)
        return c


def _evaluate(c: dict[str, Any]) -> tuple[bool, str, float]:
    today = settings.today()
    mid = c["merchant_id"]
    store = _seed().get(mid, {})
    rules = load_policies()[mid].rules
    order = store.get("orders", {}).get(c.get("order_id", ""))
    if not order:
        return False, "Order not found.", 0.0
    if not c.get("message"):
        return False, "Claim needs a description.", 0.0
    kind = c.get("type")
    if kind == "price_adjustment":
        item = next((i for i in order["items"] if i["sku"] == c.get("sku")), None)
        rule = rules.get("price_adjustment")
        if not item or not rule:
            return False, "Item not eligible for price adjustment.", 0.0
        if "marketplace" in item["seller"].lower():
            return False, "Marketplace items are excluded.", 0.0
        age = (today - date.fromisoformat(order["order_date"])).days
        if age > rule["window_days"]:
            return False, f"Purchased {age} days ago; window is {rule['window_days']} days.", 0.0
        diff = round(item["price"] - store["catalog"][item["sku"]]["price"], 2)
        if diff <= 0:
            return False, "Price has not dropped.", 0.0
        return True, "Price difference refunded.", min(diff, float(c.get("amount", diff)))
    if kind in ("duplicate_order", "return"):
        rule = rules.get("duplicate_order") or rules.get("return")
        delivered = date.fromisoformat(order["delivered"])
        if (today - delivered).days > rule["window_days"]:
            return False, "Return window has closed.", 0.0
        total = sum(i["price"] for i in order["items"])
        return True, "Return authorized; prepaid label emailed. Refund issued on receipt scan.", total
    if kind == "billing_error":
        txns = order.get("transactions", [])
        if len(txns) < 2:
            return False, "Only one charge found for this order.", 0.0
        extra = round(sum(t["amount"] for t in txns[1:]), 2)
        return True, f"Duplicate charge {txns[-1]['id']} reversed.", min(extra, float(c.get("amount", extra)))
    return False, f"Unknown claim type {kind}.", 0.0
