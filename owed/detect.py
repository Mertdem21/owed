"""Find money the user may be owed: price drops, duplicate orders, duplicate charges."""
from __future__ import annotations

from . import merchant_client as merchant
from .models import Opportunity, Order


def find_opportunities(orders: list[Order]) -> list[Opportunity]:
    found: list[Opportunity] = []

    # 1) Price dropped after purchase (live price from the merchant)
    for o in orders:
        for item in o.items:
            live = merchant.current_price(o.merchant_id, item.sku)
            if live and live["price"] < item.price - 0.009:
                diff = round(item.price - live["price"], 2)
                found.append(Opportunity(
                    id=f"{o.order_id}:{item.sku}:price", kind="price_adjustment", merchant_id=o.merchant_id,
                    merchant=o.merchant, order_id=o.order_id, sku=item.sku, item_name=item.name, amount=diff,
                    facts={"paid": item.price, "current_price": live["price"], "seller": item.seller,
                           "order_date": o.order_date, "item_flags": live.get("flags", "")},
                    evidence_email_ids=o.source_email_ids))

    # 2) The same item ordered twice within a day -> the later order is a duplicate
    by_key: dict[tuple, list[Order]] = {}
    for o in orders:
        if o.items and o.order_date:
            key = (o.merchant_id, tuple(sorted(i.sku for i in o.items)))
            by_key.setdefault(key, []).append(o)
    for group in by_key.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: (x.order_date, x.order_id))
        first = group[0]
        for dup in group[1:]:
            if abs((dup.order_date - first.order_date).days) <= 1:
                found.append(Opportunity(
                    id=f"{dup.order_id}:duplicate", kind="duplicate_order", merchant_id=dup.merchant_id,
                    merchant=dup.merchant, order_id=dup.order_id, item_name=", ".join(i.name for i in dup.items),
                    amount=round(sum(i.price * i.qty for i in dup.items), 2),
                    facts={"original_order": first.order_id, "order_date": dup.order_date,
                           "delivered": dup.delivered},
                    evidence_email_ids=first.source_email_ids + dup.source_email_ids))

    # 3) Charged more than once for one order. Only an identical amount within a few days counts as a duplicate:
    #    split shipments or partial charges are legitimate and must not be claimed back.
    for o in orders:
        if len(o.transactions) < 2:
            continue
        seen: list = []
        extra = []
        for t in sorted(o.transactions, key=lambda t: (t.date or o.order_date or t.id, t.id)):
            twin = next((s for s in seen if abs(s.amount - t.amount) < 0.01 and
                         (not s.date or not t.date or abs((t.date - s.date).days) <= 3)), None)
            (extra if twin else seen).append(t)
        if extra:
            found.append(Opportunity(
                id=f"{o.order_id}:double-charge", kind="billing_error", merchant_id=o.merchant_id,
                merchant=o.merchant, order_id=o.order_id, item_name=", ".join(i.name for i in o.items),
                amount=round(sum(t.amount for t in extra), 2),
                facts={"transactions": [t.id for t in o.transactions], "duplicate_transactions": [t.id for t in extra],
                       "charge_date": extra[-1].date},
                evidence_email_ids=o.source_email_ids))
    return found
