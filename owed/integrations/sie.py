"""Superlinked SIE: small task-specific models instead of a frontier LLM.

- extract(): GLiNER entity extraction on every receipt email (order number, merchant, product, price, dates)
- rerank():  cross-encoder reranking of policy passages, so the best clause is cited

SDK (sie-sdk 0.11, github.com/superlinked/sie):
    from sie_sdk import SIEClient
    client = SIEClient(base_url, api_key=...)              # SIE Serverless: https://api.superlinked.com
    client.extract(model, {"text": ...}, labels=[...])      -> {"entities": [{text, label, score, start, end}]}
    client.score(model, {"text": q}, [{"id":..,"text":..}]) -> {"scores": [{item_id, score, rank}]}

Every call is capped (provision_timeout_s) so a cold serverless start never stalls the agent;
on failure the agent falls back to its deterministic path and the audit trail says so.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from ..config import settings

log = logging.getLogger("owed.sie")
RECEIPT_LABELS = ["merchant", "order number", "product name", "price", "order date", "transaction id", "seller"]
PROVISION_TIMEOUT_S = float(os.getenv("SIE_PROVISION_TIMEOUT_S", "60"))
REQUEST_TIMEOUT_S = float(os.getenv("SIE_TIMEOUT_S", "45"))

_client: Any = None
_client_error: str = ""
_last_error: str = ""
_working_rerank: str = ""


def enabled() -> bool:
    return bool(settings.sie_api_key)


def last_error() -> str:
    return _last_error or _client_error


def _get_client() -> Any:
    global _client, _client_error
    if _client is not None or _client_error:
        return _client
    try:
        from sie_sdk import SIEClient  # type: ignore
    except ImportError:
        _client_error = "sie-sdk not installed (uv sync --extra sponsors)"
        log.warning(_client_error)
        return None
    try:
        _client = SIEClient(settings.sie_base_url, api_key=settings.sie_api_key, timeout_s=REQUEST_TIMEOUT_S)
    except TypeError:
        try:
            _client = SIEClient(settings.sie_base_url, api_key=settings.sie_api_key)
        except Exception as exc:  # noqa: BLE001
            _client_error = f"SIEClient init failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        _client_error = f"SIEClient init failed: {exc}"
    if _client_error:
        log.warning(_client_error)
    return _client


def _call(fn, *args, **kwargs):
    """Call an SDK method with a capped provisioning wait; tolerate SDKs without that kwarg."""
    try:
        return fn(*args, provision_timeout_s=PROVISION_TIMEOUT_S, **kwargs)
    except TypeError as exc:
        if "provision_timeout_s" not in str(exc):
            raise
        return fn(*args, **kwargs)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def extract(text: str, labels: list[str] | None = None) -> list[dict[str, Any]] | None:
    """Returns entities or None when SIE is not available / fails."""
    global _last_error
    if not enabled():
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        res = _call(client.extract, settings.sie_extract_model, {"text": text[:4000]}, labels=labels or RECEIPT_LABELS)
        if isinstance(res, list):
            res = res[0] if res else {}
        ents = _get(res, "entities", []) or []
        return [{"text": _get(e, "text"), "label": _get(e, "label"), "score": round(float(_get(e, "score", 0) or 0), 3)}
                for e in ents]
    except Exception as exc:  # noqa: BLE001
        _last_error = f"extract({settings.sie_extract_model}): {type(exc).__name__}: {exc}"[:300]
        log.warning("SIE %s", _last_error)
        return None


_discovered: list[str] | None = None
_catalog: list[str] = []


def _discover_rerankers(client: Any) -> list[str]:
    """Ask the SIE server which models can score (rerank); cached."""
    global _discovered, _catalog
    if _discovered is not None:
        return _discovered
    _discovered = []
    try:
        models = client.list_models() or []
    except Exception as exc:  # noqa: BLE001
        log.warning("SIE list_models failed: %s", exc)
        return _discovered
    _catalog = [str(_get(m, "name", "")) for m in models]
    scored = []
    for m in models:
        name = str(_get(m, "name", ""))
        outs = [str(o).lower() for o in (_get(m, "outputs", []) or [])]
        low = name.lower()
        hit = ("score" in outs) or ("rerank" in low) or ("cross-encoder" in low) or ("ms-marco" in low)
        if hit:
            scored.append((0 if "rerank" in low else 1, name))
    _discovered = [n for _, n in sorted(scored)]
    return _discovered


def _rerank_models(client: Any = None) -> list[str]:
    seen: list[str] = []
    for m in [_working_rerank, settings.sie_rerank_model]:
        if m and m not in seen:
            seen.append(m)
    if client is not None and not _working_rerank:
        for m in _discover_rerankers(client):
            if m not in seen:
                seen.append(m)
    for m in ["BAAI/bge-reranker-v2-m3", "cross-encoder/ms-marco-MiniLM-L-6-v2"]:
        if m not in seen:
            seen.append(m)
    return seen[:6]


def rerank(query: str, docs: list[str]) -> list[float] | None:
    """Returns one score per doc (same order) or None."""
    global _last_error, _working_rerank
    if not enabled() or not docs:
        return None
    client = _get_client()
    if client is None:
        return None
    items = [{"id": f"doc-{i}", "text": d[:2000]} for i, d in enumerate(docs)]
    for model in _rerank_models(client):
        try:
            res = _call(client.score, model, {"text": query}, items)
        except Exception as exc:  # noqa: BLE001
            _last_error = f"score({model}): {type(exc).__name__}: {exc}"[:300]
            log.warning("SIE %s", _last_error)
            if _working_rerank:  # a model already worked; this is a transient error, don't loop
                return None
            continue
        rows = _get(res, "scores", []) or []
        out = [float("-inf")] * len(docs)
        for r in rows:
            m = re.search(r"(\d+)$", str(_get(r, "item_id", "")))
            if m and int(m.group(1)) < len(docs):
                out[int(m.group(1))] = float(_get(r, "score", 0) or 0)
        _working_rerank = model
        return out
    return None


def rerank_model() -> str:
    return _working_rerank or settings.sie_rerank_model


def smoke() -> str:
    if not enabled():
        return "SKIP: SIE_API_KEY not set"
    ents = extract("Northwind Electronics order NW-48213 placed November 12, 2026: Aurora headphones $249.99")
    if ents is None:
        return f"FAIL: extract - {last_error() or 'unknown'}"
    scores = rerank("refund the difference after a price drop",
                    ["We refund the difference if the price drops within 14 days.", "Returns need original packaging."])
    if scores:
        rr = f"rerank[{rerank_model()}]={[round(s, 3) for s in scores]}"
    else:
        rr = (f"rerank FAIL - {last_error()} | reranker candidates={_discovered} | "
              f"catalog({len(_catalog)})={_catalog[:40]}")
    return f"OK: extract[{settings.sie_extract_model}] {len(ents)} entities {[(e['label'], e['text']) for e in ents][:5]} | {rr}"
