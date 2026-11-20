"""Command line:  uv run python -m owed.cli run [--reset] [--inbox test]   |   uv run python -m owed.cli smoke [name]"""
from __future__ import annotations

import argparse
import logging

from .config import settings


def cmd_run(reset: bool, inbox: str | None = None) -> None:
    from .agent import run

    print(f"Integrations: {settings.enabled()}\n")
    out = run(reset=reset, verbose=True, inbox=inbox)
    print(f"\nRecovered ${out['recovered']:.2f} | {out['events']} audit events -> {out['storage']} | "
          f"Amplitude events sent: {out['amplitude_events']}")
    for c in out["cases"]:
        print(f"  {c['status']:<15} {c['opportunity']['kind']:<17} {c['opportunity']['item_name'][:38]:<38} "
              f"${c['amount_recovered'] or c['opportunity']['amount']:.2f}")


def cmd_smoke(name: str | None) -> None:
    from .integrations import amplitude, arcee, cognee_memory, gmail_airbyte, gmail_imap, senso, sie, statsig_gate, store

    checks = {"arcee": arcee.smoke, "senso": senso.smoke, "sie": sie.smoke, "statsig": statsig_gate.smoke,
              "amplitude": amplitude.smoke, "singlestore": store.smoke, "cognee": cognee_memory.smoke,
              "gmail": gmail_airbyte.smoke, "gmail_imap": gmail_imap.smoke}
    for key, fn in checks.items():
        if name and name != key:
            continue
        try:
            print(f"{key:<12} {fn()}")
        except Exception as exc:  # noqa: BLE001
            print(f"{key:<12} FAIL: {exc}")


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="owed")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--reset", action="store_true", help="clear previous cases and claims first")
    r.add_argument("--inbox", help="run on a local inbox from data/inbox, e.g. 'test' (edge cases) or 'demo'")
    sub.add_parser("memory-reset", help="clear Cognee memory and load the demo history")
    s = sub.add_parser("smoke")
    s.add_argument("name", nargs="?", help="arcee | senso | sie | statsig | amplitude | singlestore | cognee | gmail")
    a = p.parse_args()
    if a.cmd == "run":
        cmd_run(a.reset, a.inbox)
    elif a.cmd == "memory-reset":
        from .integrations import cognee_memory

        print(cognee_memory.reset_memory())
    else:
        cmd_smoke(a.name)


if __name__ == "__main__":
    main()
