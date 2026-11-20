"""Runs the agent on the edge-case test inbox (data/inbox/test_inbox.json) and compares every case with the expected result.

Run: uv run python -m tests.test_inbox
"""
from __future__ import annotations

from owed import agent

# case id -> (expected status, expected amount, why)
EXPECTED = {
    "NW-49001:NW-VTX-KB:price": ("verified", 30.00, "price drop inside the 14-day window"),
    "NW-49002:NW-HLO-CAM:price": ("verified", 15.00, "last day of the 14-day window"),
    "NW-49003:NW-DRF-2T:price": ("declined", 20.00, "window missed by one day"),
    "NW-49004:NW-PLS-SPK:price": ("declined", 20.00, "clearance item is excluded"),
    "NW-49005:NW-NMB-11:price": ("declined", 30.00, "open-box item is excluded"),
    "NW-49006:NW-LUM-34:price": ("needs_approval", 170.00, "above the $150 autonomy limit"),
    "CO-91001:CO-SDV-L-BLK:price": ("declined", 30.00, "store has no price-adjustment policy"),
    "BH-4980:double-charge": ("declined", 64.00, "double charge reported after the 60-day window"),
}
NOT_DETECTED = {
    "BH-5610:double-charge": "two different amounts = split charge, not a double charge",
    "CO-91024:duplicate": "same socks ordered 2 days apart = intentional reorder, not a duplicate",
}


def main() -> int:
    out = agent.run(reset=True, inbox="test")
    cases = {c["id"]: c for c in out["cases"]}
    ok = 0
    rows = []
    for cid, (status, amount, why) in EXPECTED.items():
        c = cases.get(cid)
        got = c["status"] if c else "missing"
        amt = (c["amount_recovered"] or c["opportunity"]["amount"]) if c else 0
        good = got == status and abs(amt - amount) < 0.01
        ok += good
        rows.append(("PASS" if good else "FAIL", cid, f"{status} ${amount:.2f}", f"{got} ${amt:.2f}", why))
    for cid, why in NOT_DETECTED.items():
        good = cid not in cases
        ok += good
        rows.append(("PASS" if good else "FAIL", cid, "not flagged", "not flagged" if good else cases[cid]["status"], why))
    unexpected = set(cases) - set(EXPECTED)
    for cid in sorted(unexpected):
        rows.append(("FAIL", cid, "-", cases[cid]["status"], "unexpected case"))
    total = len(EXPECTED) + len(NOT_DETECTED) + len(unexpected)
    for r in rows:
        print(f"{r[0]}  {r[1]:<30} expected {r[2]:<24} got {r[3]:<24} {r[4]}")
    print(f"\n{ok}/{total} correct | recovered ${out['recovered']:.2f} | phishing + newsletter ignored")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
