"""Retrieve the policy passages (Senso), rank them (Superlinked SIE) and read the rule (Arcee).

Two independent readings of every rule: the LLM's reading of the retrieved text, and the merchant policy registry.
The quoted clause must appear verbatim in a retrieved passage, otherwise it is marked unverified.
"""
from __future__ import annotations

import logging
from typing import Any

from .integrations import arcee, senso, sie
from .models import Evidence, Opportunity, Rule
from .policies import KIND_KEYWORDS, load_policies, local_search, normalize, sentences

log = logging.getLogger("owed.policy")


def retrieve(op: Opportunity) -> tuple[list[Evidence], dict[str, Any]]:
    query = KIND_KEYWORDS[op.kind]
    meta: dict[str, Any] = {"query": query}
    evidence: list[Evidence] = []
    if senso.enabled():
        try:
            evidence = senso.search(op.merchant, op.merchant_id, query)
            meta["retriever"] = "senso"
        except Exception as exc:  # noqa: BLE001
            meta["senso_error"] = str(exc)[:200]
    if not evidence:
        evidence = [Evidence(provider="local-policy", source_id=s.source_id, title=s.title, text=s.text, score=sc)
                    for s, sc in local_search(op.merchant_id, query, k=4)]
        meta["retriever"] = "local-policy" + (" (senso returned nothing)" if senso.enabled() else "")
    scores = sie.rerank(query, [e.text for e in evidence])
    if scores is not None:
        for e, s in zip(evidence, scores):
            e.score = round(s, 3)
        evidence.sort(key=lambda e: e.score or 0, reverse=True)
        meta["reranker"] = "superlinked-sie"
    return evidence, meta


def _verify_quote(quote: str, evidence: list[Evidence]) -> bool:
    q = normalize(quote)
    return bool(q) and any(q in normalize(e.text) for e in evidence)


def _best_sentence(kind: str, evidence: list[Evidence]) -> str:
    cues = {"price_adjustment": ["lowers the price", "difference"], "duplicate_order": ["duplicate"],
            "billing_error": ["more than once", "duplicate charge"]}[kind]
    for e in evidence:
        for s in sentences(e.text):
            if any(c in s.lower() for c in cues):
                return s
    return sentences(evidence[0].text)[0] if evidence else ""


def interpret(op: Opportunity, evidence: list[Evidence]) -> tuple[Rule, dict[str, Any]]:
    registry = load_policies()[op.merchant_id].rules.get(op.kind) or {}
    meta: dict[str, Any] = {"registry": registry}
    rule = Rule(kind=op.kind, window_days=registry.get("window_days"), anchor=registry.get("anchor", "purchase_date"),
                excludes=list(registry.get("excludes", [])), provider="policy-registry")
    if arcee.enabled() and evidence:
        try:
            parsed, llm_meta = arcee.interpret_rule(op.kind, op.merchant, [e.text for e in evidence[:3]])
            meta["llm"] = llm_meta
            meta["llm_output"] = parsed
            quote = str(parsed.get("clause_quote") or "")
            llm_days = parsed.get("window_days")
            rule.provider = f"arcee:{llm_meta['model']}"
            rule.clause_quote = quote
            rule.citation_verified = _verify_quote(quote, evidence)
            if parsed.get("applies") is False:
                rule.note = "Model read the policy as not covering this case."
                rule.window_days = 0
            elif isinstance(llm_days, int) and registry.get("window_days") and llm_days != registry["window_days"]:
                rule.window_days = min(llm_days, registry["window_days"])
                rule.note = f"Readings disagree (model {llm_days}d vs registry {registry['window_days']}d) - using the stricter one."
            elif isinstance(llm_days, int):
                rule.window_days = llm_days
            if parsed.get("anchor") in ("purchase_date", "delivery_date", "charge_date"):
                rule.anchor = parsed["anchor"]
            for ex in parsed.get("excludes") or []:
                if isinstance(ex, str) and ex.lower() not in [x.lower() for x in rule.excludes]:
                    rule.excludes.append(ex.lower())
        except Exception as exc:  # noqa: BLE001
            meta["llm_error"] = str(exc)[:300]
            log.warning("Arcee rule reading failed, using registry: %s", exc)
    if not rule.clause_quote or not rule.citation_verified:
        fallback = _best_sentence(op.kind, evidence)
        if rule.clause_quote and not rule.citation_verified:
            meta["unverified_quote"] = rule.clause_quote
        rule.clause_quote = fallback
        rule.citation_verified = _verify_quote(fallback, evidence)
    return rule, meta
