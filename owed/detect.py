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
                           "order_date": o.order_date},
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

    # 3) Charged more than once for one order
    for o in orders:
        if len(o.transactions) > 1:
            extra = o.transactions[1:]
            found.append(Opportunity(
                id=f"{o.order_id}:double-charge", kind="billing_error", merchant_id=o.merchant_id,
                merchant=o.merchant, order_id=o.order_id, item_name=", ".join(i.name for i in o.items),
                amount=round(sum(t.amount for t in extra), 2),
                facts={"transactions": [t.id for t in o.transactions], "charge_date": o.transactions[-1].date},
                evidence_email_ids=o.source_email_ids))
    return found
