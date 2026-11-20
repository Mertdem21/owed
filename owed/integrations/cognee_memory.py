"""Cognee: long-term memory of every claim Owed has made, as a knowledge graph.

- recall(merchant): before acting, the agent asks its memory what happened in earlier claims with that store
  (outcomes, refund ids, rejections). The answer is shown in the audit trail next to the decision.
- remember(cases): after a run, every outcome is written back, so the next run starts with more context.

Memory is context, never a decision: eligibility is still decided in code from the cited policy sentence.

Cognee Cloud REST (per-tenant base URL from the API Keys page, header X-Api-Key):
    POST /api/v1/remember  multipart: raw_data=<text>..., datasetName=..., run_in_background=true  (add + cognify)
    POST /api/v1/search    {"query": ..., "search_type": "GRAPH_COMPLETION", "datasets": [...]}
    GET  /api/v1/datasets/
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from ..config import settings

log = logging.getLogger("owed.cognee")
SEARCH_TIMEOUT_S = 30
_cache: dict[str, dict[str, Any]] = {}
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def enabled() -> bool:
    return bool(settings.cognee_api_key and settings.cognee_base_url)


def _base() -> str:
    return settings.cognee_base_url.rstrip("/")


def _headers(json_body: bool = False) -> dict[str, str]:
    h = {"X-Api-Key": settings.cognee_api_key}
    if settings.cognee_tenant_id:
        h["X-Tenant-Id"] = settings.cognee_tenant_id
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _client():
    import httpx

    return httpx


def reset_cache() -> None:
    with _guard:
        _cache.clear()
        _locks.clear()


def _text_of(res: Any, limit: int = 4) -> str:
    """Pull readable text out of whatever shape the search endpoint returns."""
    out: list[str] = []

    def walk(x: Any) -> None:
        if len(out) >= limit or x is None:
            return
        if isinstance(x, str):
            if x.strip():
                out.append(x.strip())
        elif isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            for key in ("search_result", "result", "answer", "completion", "text", "content", "results", "data"):
                if key in x:
                    walk(x[key])
                    return
    walk(res)
    return "\n".join(out)


def search(query: str) -> tuple[str, dict[str, Any]]:
    httpx = _client()
    body = {"query": query, "search_type": "GRAPH_COMPLETION", "datasets": [settings.cognee_dataset]}
    r = httpx.post(f"{_base()}/api/v1/search", headers=_headers(True), json=body, timeout=SEARCH_TIMEOUT_S)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    res = r.json()
    return _text_of(res), {"query": query, "search_type": body["search_type"], "dataset": settings.cognee_dataset,
                           "raw_sample": str(res)[:600]}


def recall(merchant: str) -> dict[str, Any]:
    """Memory about earlier claims with one store. Cached per run, so parallel cases share one lookup."""
    if not enabled():
        return {"ok": False, "answer": "", "note": "Cognee not configured"}
    with _guard:
        lock = _locks.setdefault(merchant, threading.Lock())
    with lock:
        if merchant in _cache:
            return {**_cache[merchant], "cached": True}
        query = (f"What happened in earlier refund claims that Owed made with {merchant}? "
                 f"Give outcomes, amounts and refund ids in at most two sentences. "
                 f"If there were none, answer exactly: No earlier claims.")
        try:
            answer, meta = search(query)
            out = {"ok": True, "answer": answer or "No earlier claims.", **meta}
        except Exception as exc:  # noqa: BLE001 - empty dataset or network issue: the agent works without memory
            msg = str(exc)
            first_run = any(s in msg.lower() for s in ("not found", "no data", "does not exist", "404", "409"))
            out = {"ok": first_run, "answer": "No earlier claims (memory is empty)." if first_run else "",
                   "error": msg[:300], "query": query}
        _cache[merchant] = out
        return out


def remember(texts: list[str]) -> dict[str, Any]:
    """Write outcomes to memory. Runs add + cognify in the background on Cognee's side."""
    if not enabled() or not texts:
        return {"ok": False, "note": "nothing to remember" if enabled() else "Cognee not configured"}
    httpx = _client()
    fields = [("raw_data", (None, t)) for t in texts]
    fields += [("datasetName", (None, settings.cognee_dataset)), ("run_in_background", (None, "true"))]
    r = httpx.post(f"{_base()}/api/v1/remember", headers=_headers(), files=fields, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        body = r.json()
    except ValueError:
        body = {"text": r.text[:200]}
    return {"ok": True, "items": len(texts), "dataset": settings.cognee_dataset,
            "pipeline_run_id": body.get("pipeline_run_id") if isinstance(body, dict) else None,
            "raw_sample": str(body)[:300]}


def smoke() -> str:
    if not enabled():
        return "SKIP: COGNEE_API_KEY / COGNEE_BASE_URL not set"
    httpx = _client()
    r = httpx.get(f"{_base()}/api/v1/datasets/", headers=_headers(), timeout=20)
    if r.status_code >= 400:
        return f"FAIL: datasets HTTP {r.status_code}: {r.text[:200]}"
    names = [d.get("name") for d in r.json()] if isinstance(r.json(), list) else r.json()
    if settings.cognee_dataset not in (names if isinstance(names, list) else []):
        return f"OK: connected, datasets={names} (memory dataset '{settings.cognee_dataset}' is created on the first run)"
    try:
        answer, _ = search("Which stores has Owed filed claims with, and what were the outcomes?")
        return f"OK: datasets={names} | memory says: {answer[:200]}"
    except Exception as exc:  # noqa: BLE001
        return f"PARTIAL: connected, datasets={names}, search failed: {exc}"
