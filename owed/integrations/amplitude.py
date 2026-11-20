"""Amplitude: every agent step is sent as an event, so the whole run shows up as a session in Amplitude.

Uses the Amplitude HTTP V2 API (no SDK required). LLM calls are additionally wrapped with the
`amplitude-ai` SDK in arcee.py when that SDK is installed.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..config import settings
from ._http import IntegrationError, request

log = logging.getLogger("owed.amplitude")
AGENT_ID = "owed-money-back-agent"
_queue: list[dict[str, Any]] = []


def enabled() -> bool:
    return bool(settings.amplitude_api_key)


def track(event_type: str, run_id: str, props: dict[str, Any] | None = None) -> None:
    if not enabled():
        return
    event = {
        "user_id": settings.user_id,
        "event_type": event_type,
        "time": int(time.time() * 1000),
        "session_id": _session_number(run_id),
        "event_properties": {
            "[Agent] Session ID": run_id,
            "[Agent] Agent ID": AGENT_ID,
            **(props or {}),
        },
    }
    _queue.append(event)


def track_step(run_id: str, step: str, provider: str, summary: str, case_id: str | None,
               duration_ms: int | None, ok: bool = True) -> None:
    track("[Agent] Tool Call", run_id, {
        "[Agent] Tool Name": step,
        "[Agent] Latency Ms": duration_ms or 0,
        "[Agent] Is Error": not ok,
        "provider": provider,
        "case_id": case_id or "",
        "summary": summary[:500],
    })


def flush() -> int:
    """Send queued events. Returns number of events sent (0 when disabled or on error)."""
    global _queue
    if not enabled() or not _queue:
        _queue = []
        return 0
    batch, _queue = _queue, []
    try:
        request("POST", "https://api2.amplitude.com/2/httpapi",
                json={"api_key": settings.amplitude_api_key, "events": batch}, timeout=10)
        return len(batch)
    except IntegrationError as exc:
        log.warning("Amplitude flush failed: %s", exc)
        return 0


def _session_number(run_id: str) -> int:
    # Amplitude session_id must be a number (ms timestamp). Run ids start with a ms timestamp.
    try:
        return int(run_id.split("-")[1])
    except (IndexError, ValueError):
        return int(time.time() * 1000)


def smoke() -> str:
    if not enabled():
        return "SKIP: AMPLITUDE_API_KEY not set"
    run_id = f"run-{int(time.time() * 1000)}-smoke"
    track("Owed Smoke Test", run_id, {"hello": "hackathon"})
    sent = flush()
    return f"OK: sent {sent} event(s) to Amplitude" if sent else "FAIL: see warning above"
