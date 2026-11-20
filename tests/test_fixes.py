"""Edge cases for idempotency, policy exclusions and double-charge detection.

Run: uv run python -m tests.test_fixes   (no pytest needed; also works with pytest)
"""
from __future__ import annotations

from datetime import date

from owed import agent
from owed.decide import decide
from owed.detect import find_opportunities
from owed.models import LineItem, Opportunity, Order, Rule, Transaction

TODAY = date(2026, 11, 20)
NW_RULE = dict(kind="price_adjustment", window_days=14, anchor="purchase_date",
               excludes=["marketplace", "clearance", "open_box"], clause_quote="x", citation_verified=True)


def _price_op(name: str, seller: str = "Northwind Electronics", flags: str = "") -> Opportunity:
    return Opportunity(id=f"T:{name}", kind="price_adjustment", merchant_id="northwind", merchant="Northwind Electronics",
                       order_id="T-1", sku="T", item_name=name, amount=20.0,
                       facts={"seller": seller, "order_date": date(2026, 11, 15), "item_flags": flags})


def test_clearance_and_open_box_are_declined():
    rule = Rule(**NW_RULE)
    assert decide(_price_op("Aurora Headphones"), rule, TODAY).eligible
    assert not decide(_price_op("Bluetooth Speaker (Clearance)"), rule, TODAY).eligible
    assert not decide(_price_op("Monitor", flags="Open-Box"), rule, TODAY).eligible
    assert not decide(_price_op("USB-C Hub", seller="GadgetHub (Marketplace seller)"), rule, TODAY).eligible


def _order(txns: list[tuple[str, float]]) -> Order:
    return Order(merchant_id="brightline", merchant="Brightline Home", order_id="BH-T", order_date=date(2026, 11, 10),
                 items=[LineItem(sku="NOPE", name="Lamp", price=100.0)],
                 transactions=[Transaction(id=i, amount=a, date=date(2026, 11, 10)) for i, a in txns])


def test_split_charges_are_not_a_double_charge():
    ops = [o for o in find_opportunities([_order([("T1", 60.0), ("T2", 40.0)])]) if o.kind == "billing_error"]
    assert ops == []


def test_identical_charges_are_a_double_charge():
    ops = [o for o in find_opportunities([_order([("T1", 84.5), ("T2", 84.5)])]) if o.kind == "billing_error"]
    assert len(ops) == 1 and ops[0].amount == 84.5


def test_second_run_files_nothing_twice():
    first = agent.run(reset=True)
    second = agent.run(reset=False)
    assert first["recovered"] > 0
    assert second["recovered"] == 0
    assert second["skipped_already_handled"] == sum(1 for c in first["cases"] if c["status"] in agent.ALREADY_HANDLED)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("all edge-case tests passed")
