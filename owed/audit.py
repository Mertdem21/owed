"""Audit trail: every step the agent takes is recorded with its provider, inputs, sources and timing.

Events go to the store (SingleStore or SQLite) and to Amplitude.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from .integrations import amplitude
from .integrations.store import get_store
from .models import AuditEvent

log = logging.getLogger("owed.audit")


def new_run_id() -> str:
    return f"run-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"


class Recorder:
    def __init__(self, run_id: str, verbose: bool = False) -> None:
        self.run_id = run_id
        self.verbose = verbose
        self.events: list[AuditEvent] = []

    def step(self, case_id: str | None, step: str, provider: str, summary: str,
             data: dict[str, Any] | None = None, t0: float | None = None, ok: bool = True) -> AuditEvent:
        duration = int((time.time() - t0) * 1000) if t0 else None
        e = AuditEvent(run_id=self.run_id, case_id=case_id, step=step, provider=provider, summary=summary,
                       data=data or {}, duration_ms=duration)
        self.events.append(e)
        try:
            get_store().add_event(e)
        except Exception as exc:  # noqa: BLE001 - never lose the run because of storage
            log.warning("could not store audit event: %s", exc)
        amplitude.track_step(self.run_id, step, provider, summary, case_id, duration, ok)
        if self.verbose:
            tag = f"[{case_id}]" if case_id else ""
            print(f"  {step:<16} {provider:<28} {tag} {summary}")
        return e
