"""FastAPI app: agent API, audit trail UI and the mock merchant API.

    uv run uvicorn owed.app:app --reload --port 8000     ->  http://localhost:8000
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import agent, merchant
from .audit import new_run_id
from .config import ROOT, settings
from .integrations import arcee, gmail_airbyte
from .integrations.store import get_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
app = FastAPI(title="Owed - money-back agent", version="0.1.0")
UI = ROOT / "ui" / "index.html"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(UI)


# --- agent API --------------------------------------------------------------------------------
@app.get("/api/status")
def status() -> dict[str, Any]:
    integrations = {**settings.enabled(), "arcee": arcee.healthy()}  # a key without credits is not "live"
    inbox = "gmail" if integrations.get("gmail") else "demo inbox"
    return {"integrations": integrations, "storage": get_store().backend, "today": settings.today().isoformat(),
            "inbox": inbox, "gmail_address": settings.gmail_address if integrations.get("gmail") else "",
            "arcee_models": {k: v for k, v in arcee._models.items() if k != "available"}}


def _check_inbox(inbox: str | None) -> str | None:
    if inbox and inbox not in gmail_airbyte.local_inboxes():
        raise HTTPException(400, f"unknown inbox '{inbox}' - available: {', '.join(gmail_airbyte.local_inboxes())}")
    return inbox or None


@app.post("/api/run")
def run_agent(reset: bool = True, inbox: str | None = None) -> dict[str, Any]:
    """inbox: run on a local test inbox from data/inbox (e.g. 'test') instead of Gmail / the demo inbox."""
    return agent.run(reset=reset, inbox=_check_inbox(inbox))


_runs: dict[str, dict[str, Any]] = {}
_run_lock = threading.Lock()


@app.post("/api/run/start")
def run_start(reset: bool = True, inbox: str | None = None) -> dict[str, Any]:
    """Start a run in the background; the UI follows it live through /api/runs/{run_id}/feed."""
    inbox = _check_inbox(inbox)
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "a run is already in progress")
    run_id = new_run_id()
    _runs[run_id] = {"done": False}

    def work() -> None:
        try:
            res = agent.run(reset=reset, run_id=run_id, inbox=inbox)
            _runs[run_id] = {"done": True, "recovered": res["recovered"], "storage": res["storage"],
                             "amplitude_events": res["amplitude_events"], "events_total": res["events"]}
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("owed.app").exception("run failed")
            _runs[run_id] = {"done": True, "error": str(exc)[:300]}
        finally:
            _run_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}/feed")
def run_feed(run_id: str) -> dict[str, Any]:
    """Every step of a run so far (small fields only), for the live view."""
    feed = []
    for e in get_store().events(run_id=run_id):
        d = e.get("data") or {}
        item = {k: e.get(k) for k in ("case_id", "ts", "step", "provider", "summary", "duration_ms")}
        if e["step"] == "detected":
            item["opportunity"] = d.get("opportunity")
        if e["step"] == "verification":
            item["refund"] = (d.get("status") or {}).get("refund_amount")
        if e["step"] == "ingest":
            item["emails"] = d.get("emails") or [{"id": str(i), "subject": sub} for i, sub in enumerate(d.get("subjects") or [])]
        if e["step"] == "extract":
            item["report"] = [{k: r.get(k) for k in ("email_id", "subject", "receipt", "merchant", "order_id", "sie_cross_check")}
                              for r in d.get("report") or []]
        if e["step"] == "prices":
            item["checks"] = d.get("checks") or []
        feed.append(item)
    return {"run_id": run_id, **_runs.get(run_id, {"done": True}), "events": feed}


@app.get("/api/export")
def export_run() -> dict[str, Any]:
    """Snapshot of the latest run (status, live feed, cases, case details) for the hosted replay page.
    Written to data/runtime/replay.json. Contains no keys."""
    import json as _json
    from datetime import datetime, timezone

    from .config import RUNTIME

    run_id = get_store().latest_run_id()
    cs = cases()
    data = {"recorded_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id, "status": status(),
            "run": _runs.get(run_id or "", {"done": True}), "feed": run_feed(run_id)["events"] if run_id else [],
            "recovered": cs["recovered"], "cases": cs["cases"],
            "details": {c["id"]: case_detail(c["id"]) for c in cs["cases"]}}
    text = _json.dumps(data, default=str)
    (RUNTIME / "replay.json").write_text(text)
    return {"ok": True, "run_id": run_id, "bytes": len(text), "cases": len(cs["cases"]), "feed": len(data["feed"])}


@app.get("/api/cases")
def cases() -> dict[str, Any]:
    items = [c.model_dump(mode="json") for c in get_store().cases()]
    return {"recovered": round(sum(c["amount_recovered"] for c in items), 2), "cases": items,
            "latest_run_id": get_store().latest_run_id()}


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str) -> dict[str, Any]:
    c = get_store().case(case_id)
    if not c:
        raise HTTPException(404, "case not found")
    return {"case": c.model_dump(mode="json"), "events": get_store().events(case_id=case_id)}


@app.post("/api/cases/{case_id}/approve")
def approve(case_id: str) -> dict[str, Any]:
    c = agent.approve(case_id)
    if not c:
        raise HTTPException(404, "case not found")
    return c.model_dump(mode="json")


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str) -> list[dict[str, Any]]:
    return [e for e in get_store().events(run_id=run_id) if not e["case_id"]]


class SQL(BaseModel):
    sql: str


@app.post("/api/sql")
def sql(q: SQL) -> dict[str, Any]:
    """Read-only SQL over the audit trail (SingleStore or SQLite) - for the demo."""
    try:
        rows = get_store().query(q.sql)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {"backend": get_store().backend, "rows": [list(r) for r in rows[:200]]}


# --- mock merchant API ------------------------------------------------------------------------
class ClaimIn(BaseModel):
    type: str
    order_id: str
    sku: str | None = None
    amount: float
    message: str


@app.get("/merchant/{merchant_id}/products/{sku}")
def m_price(merchant_id: str, sku: str) -> dict[str, Any]:
    p = merchant.current_price(merchant_id, sku)
    if not p:
        raise HTTPException(404, "product not found")
    return p


@app.get("/merchant/{merchant_id}/orders/{order_id}")
def m_order(merchant_id: str, order_id: str) -> dict[str, Any]:
    o = merchant.get_order(merchant_id, order_id)
    if not o:
        raise HTTPException(404, "order not found")
    return o


@app.post("/merchant/{merchant_id}/claims", status_code=201)
def m_claim(merchant_id: str, claim: ClaimIn) -> dict[str, Any]:
    return merchant.file_claim(merchant_id, claim.model_dump())


@app.get("/merchant/claims/{claim_id}")
def m_claim_status(claim_id: str) -> dict[str, Any]:
    return merchant.claim_status(claim_id)
