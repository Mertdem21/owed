"""Eligibility is decided by code, not by the model: dates, windows, exclusions and amounts."""
from __future__ import annotations

from datetime import date, timedelta

from .models import Check, Decision, Opportunity, Rule


def _anchor(op: Opportunity, rule: Rule) -> tuple[date | None, str]:
    f = op.facts
    if rule.anchor == "delivery_date":
        return f.get("delivered"), "delivery date"
    if rule.anchor == "charge_date":
        return f.get("charge_date"), "charge date"
    return f.get("order_date"), "purchase date"


def decide(op: Opportunity, rule: Rule, today: date) -> Decision:
    checks: list[Check] = []
    checks.append(Check(name="policy covers this case", passed=bool(rule.window_days),
                        detail=rule.note or f"{rule.kind.replace('_', ' ')} rule found"))
    checks.append(Check(name="citation verified", passed=rule.citation_verified,
                        detail="quoted clause found verbatim in retrieved policy text" if rule.citation_verified
                        else "quoted clause not found in sources"))
    anchor, anchor_name = _anchor(op, rule)
    deadline = days_left = None
    if anchor and rule.window_days:
        if isinstance(anchor, str):
            anchor = date.fromisoformat(anchor)
        deadline = anchor + timedelta(days=rule.window_days)
        days_left = (deadline - today).days
        age = (today - anchor).days
        checks.append(Check(name="inside time window", passed=days_left >= 0,
                            detail=f"{anchor_name} {anchor.isoformat()} is {age} days ago; window {rule.window_days} days "
                                   f"-> deadline {deadline.isoformat()} ({days_left} days left)" if days_left >= 0 else
                                   f"{anchor_name} {anchor.isoformat()} is {age} days ago; window {rule.window_days} days "
                                   f"closed on {deadline.isoformat()}"))
    else:
        checks.append(Check(name="inside time window", passed=False, detail=f"no {anchor_name} known"))
    if op.kind == "price_adjustment":
        seller = str(op.facts.get("seller", ""))
        excluded = "marketplace" in seller.lower() and any("marketplace" in x for x in rule.excludes)
        checks.append(Check(name="seller eligible", passed=not excluded,
                            detail=f"sold by {seller}" + (" - marketplace sellers are excluded" if excluded else "")))
    checks.append(Check(name="amount owed", passed=op.amount > 0, detail=f"${op.amount:.2f}"))
    eligible = all(c.passed for c in checks)
    failed = [c for c in checks if not c.passed]
    reason = (f"Eligible: ${op.amount:.2f} can be claimed" + (f" ({days_left} days left)" if days_left is not None else "")
              if eligible else "Not eligible: " + "; ".join(f"{c.name} - {c.detail}" for c in failed))
    return Decision(eligible=eligible, reason=reason, checks=checks, deadline=deadline, days_left=days_left)
