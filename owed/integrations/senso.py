"""Senso: the trusted context layer. Merchant policies live in Senso; the agent retrieves the exact
passages (with source ids) that justify every decision.

API (docs.senso.ai): base https://apiv2.senso.ai/api/v1, header X-API-Key.
  POST /org/kb/raw            {"title", "text"}        -> 202 {id, kb_node_id}
  GET  /org/kb/nodes/{id}                              -> content.processing_status
  POST /org/search/context    {"query"}                -> {results: [{content_id, title, chunk_text, score, rank}]}
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..config import RUNTIME, settings
from ..models import Evidence
from ..policies import load_policies
from ._http import IntegrationError, request

log = logging.getLogger("owed.senso")
IDS_FILE = RUNTIME / "senso_ids.json"


def enabled() -> bool:
    return bool(settings.senso_api_key)


def _headers() -> dict[str, str]:
    return {"X-API-Key": settings.senso_api_key, "Content-Type": "application/json"}


def _url(path: str) -> str:
    return settings.senso_base_url.rstrip("/") + path


def ingest_text(title: str, text: str) -> dict[str, Any]:
    return request("POST", _url("/org/kb/raw"), json={"title": title, "text": text}, headers=_headers(), timeout=30)


def node_status(kb_node_id: str) -> str:
    data = request("GET", _url(f"/org/kb/nodes/{kb_node_id}"), headers=_headers())
    content = data.get("content") or {}
    return str(content.get("processing_status") or data.get("processing_status") or "unknown")


def seed_policies(wait: bool = True) -> dict[str, Any]:
    """Upload every policy section as its own document, so citations point at one section."""
    ids: dict[str, Any] = json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}
    pending = []
    for policy in load_policies().values():
        for section in policy.sections:
            if section.source_id in {v.get("source_id") for v in ids.values()}:
                continue
            text = f"{section.title}\n\n{section.text}"
            try:
                res = ingest_text(section.title, text)
            except IntegrationError as exc:
                if "409" in str(exc):
                    log.info("already in Senso: %s", section.title)
                    continue
                raise
            content_id = str(res.get("id") or res.get("content_id") or section.source_id)
            ids[content_id] = {"source_id": section.source_id, "merchant_id": section.merchant_id,
                               "title": section.title, "kb_node_id": res.get("kb_node_id")}
            pending.append(res.get("kb_node_id"))
            print(f"  uploaded: {section.title}")
    IDS_FILE.write_text(json.dumps(ids, indent=2))
    if wait:
        for node in [p for p in pending if p]:
            for _ in range(24):
                status = node_status(node)
                if status in ("complete", "completed", "done"):
                    break
                time.sleep(5)
    return ids


def search(merchant: str, merchant_id: str, query: str, k: int = 4) -> list[Evidence]:
    """Retrieve policy passages for one merchant from Senso. Raises IntegrationError on failure."""
    data = request("POST", _url("/org/search/context"), json={"query": f"{merchant}: {query}"},
                   headers=_headers(), timeout=30)
    ids = json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}
    out: list[Evidence] = []
    for r in data.get("results", []):
        content_id = str(r.get("content_id", ""))
        title = str(r.get("title", ""))
        known = ids.get(content_id, {})
        belongs = known.get("merchant_id") == merchant_id or merchant.lower() in title.lower()
        if not belongs:
            continue
        text = str(r.get("chunk_text", ""))
        if title and text.startswith(title):  # we ingest "title\n\nbody" - keep only the body as the passage
            text = text[len(title):].strip()
        meta = {k: r.get(k) for k in ("content_id", "kb_node_id", "version_id", "rank", "score", "retrieval_method",
                                      "retrieved_by", "method") if r.get(k) is not None}
        out.append(Evidence(provider="senso", source_id=f"senso:{content_id}", title=title or known.get("title", ""),
                            text=text, score=r.get("score"), meta=meta))
    return out[:k]


def _claims_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("claims", "results", "items", "verdicts"):
        if isinstance(data.get(key), list):
            return [c for c in data[key] if isinstance(c, dict)]
    for key in ("result", "eval", "run", "data"):
        if isinstance(data.get(key), dict):
            found = _claims_of(data[key])
            if found:
                return found
    return []


FINISHED = ("completed", "gated", "failed")


def _verdict(c: dict[str, Any]) -> str:
    for k in ("verdict", "result", "label", "classification", "outcome"):
        v = c.get(k)
        if isinstance(v, str) and v:
            return v.lower()
    if c.get("passed") is True:
        return "verified"
    if c.get("passed") is False:
        return "conflict" if c.get("conflict") or c.get("contradicted") else "unsupported"
    if c.get("about_brand") is False or c.get("verifiable") is False:
        return "not_verifiable"
    return "not_checked"


_org_name: str | None = None


def org_name() -> str:
    """Name of the Senso organisation (cached). Senso phrases fact-check claims about it."""
    global _org_name
    if _org_name is None:
        try:
            _org_name = str(request("GET", _url("/org/me"), headers=_headers()).get("name") or "")
        except Exception:  # noqa: BLE001
            _org_name = ""
    return _org_name


def check_text(text: str, question: str, max_wait_s: int = 75) -> dict[str, Any]:
    """Senso fact-check: every claim in `text` is checked against the knowledge base.
    POST /org/evals/text {text, author, question, wait} -> run; GET /org/evals/runs/{runId} until completed.
    Verdicts: verified | conflict | unsupported | not_verifiable.
    """
    body = {"text": text, "author": "ai_assistant", "question": question, "wait": True}
    t0 = time.time()
    data = request("POST", _url("/org/evals/text"), json=body, headers=_headers(), timeout=90)
    run_id = data.get("eval_run_id") or data.get("id") or data.get("run_id")
    while run_id and str(data.get("status", "")).lower() not in FINISHED and time.time() - t0 < max_wait_s:
        time.sleep(4)
        data = request("GET", _url(f"/org/evals/runs/{run_id}"), headers=_headers(), timeout=30)
    claims = _claims_of(data)
    scored_list = data.get("claims_scored") if isinstance(data.get("claims_scored"), list) else []
    scored = {c.get("claim_key") or c.get("eval_claim_id"): c for c in scored_list if isinstance(c, dict)}
    verdicts = []
    for c in claims:
        merged = {**c, **scored.get(c.get("claim_key") or c.get("eval_claim_id"), {})}
        verdicts.append({
            "claim": str(merged.get("claim_text") or merged.get("claim") or merged.get("text") or ""),
            "verdict": _verdict(merged),
            "why": str(merged.get("reasoning") or merged.get("reason") or merged.get("explanation")
                       or merged.get("rationale") or ""),
            "quote": str(merged.get("quote") or merged.get("evidence_text") or ""),
            "about_brand": merged.get("about_brand"),
        })
    if str(data.get("status", "")).lower() not in FINISHED:  # still scoring: don't guess a verdict
        for v in verdicts:
            if v["verdict"] in ("not_checked", "not_verifiable"):
                v["verdict"] = "pending"
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    return {"check_id": run_id, "status": data.get("status"),
            "accuracy_pct": data.get("accuracy_pct") or metrics.get("accuracy_pct"),
            "counts": counts, "verdicts": verdicts, "metrics": metrics,
            "raw_sample": (scored_list or claims)[:2]}


def smoke() -> str:
    if not enabled():
        return "SKIP: SENSO_API_KEY not set"
    try:
        me = request("GET", _url("/org/me"), headers=_headers())
        hits = search("Northwind Electronics", "northwind", "price adjustment after a price drop")
        return f"OK: org={me.get('name') or me.get('id') or 'ok'}, {len(hits)} Northwind passage(s) found" + (
            "" if hits else " - run `uv run python -m scripts.seed_senso` first")
    except IntegrationError as exc:
        return f"FAIL: {exc}"
