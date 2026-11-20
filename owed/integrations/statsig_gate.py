"""Statsig: decides how autonomous the agent is allowed to be.

Gate `owed_require_approval`  -> when ON, every claim waits for a human. Missing gate = OFF = fully autonomous.
Dynamic config `owed_autonomy` -> {"max_auto_claim_usd": 150}. Claims above the limit wait for a human.

Uses the Statsig HTTP API with the server secret key, so no SDK is needed.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import settings
from ._http import IntegrationError, request

log = logging.getLogger("owed.statsig")
BASE = "https://api.statsig.com/v1"
GATE = "owed_require_approval"
CONFIG = "owed_autonomy"


def enabled() -> bool:
    return bool(settings.statsig_secret)


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    return request("POST", f"{BASE}/{path}", json=body,
                   headers={"STATSIG-API-KEY": settings.statsig_secret, "Content-Type": "application/json"},
                   timeout=8)


def autonomy_policy() -> dict[str, Any]:
    """Returns {"require_approval": bool, "max_auto_claim_usd": float, "source": str}."""
    policy = {"require_approval": False, "max_auto_claim_usd": settings.default_auto_limit_usd, "source": "default"}
    if not enabled():
        return policy
    user = {"userID": settings.user_id}
    try:
        gate = _post("check_gate", {"gateName": GATE, "user": user})
        policy["require_approval"] = bool(gate.get("value", False))
        cfg = _post("get_config", {"configName": CONFIG, "user": user})
        value = cfg.get("value") or {}
        if isinstance(value, dict) and "max_auto_claim_usd" in value:
            policy["max_auto_claim_usd"] = float(value["max_auto_claim_usd"])
        policy["source"] = "statsig"
    except (IntegrationError, ValueError, TypeError) as exc:
        log.warning("Statsig unavailable, using defaults: %s", exc)
        policy["source"] = "default (statsig error)"
    return policy


def smoke() -> str:
    if not enabled():
        return "SKIP: STATSIG_SERVER_SECRET not set"
    p = autonomy_policy()
    return f"{'OK' if p['source'] == 'statsig' else 'FAIL'}: {p}"
