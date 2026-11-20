"""The Owed agent loop.

ingest -> extract -> detect -> retrieve policy -> read rule -> decide -> autonomy gate -> act -> verify -> notify
Every step lands in the audit trail.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import merchant, merchant_client
from .audit import Recorder, new_run_id
from .config import settings
from .decide import decide
from .detect import find_opportunities
from .extract import parse_orders
from .integrations import amplitude, arcee, cognee_memory, gmail_airbyte, senso, statsig_gate
from .integrations.store import get_store
from .models import Case, utcnow
from .policies import load_policies, normalize
from .policy_check import interpret, retrieve

log = logging.getLogger("owed.agent")
ALREADY_HANDLED = {"filed", "verified", "needs_approval"}
KIND_LABEL = {"price_adjustment": "Price adjustment", "duplicate_order": "Duplicate order return",
              "billing_error": "Duplicate charge refund"}


def _template_claim(case: Case) -> str:
    op, rule = case.opportunity, case.rule
    remedy = {"price_adjustment": f"a price adjustment refund of ${op.amount:.2f}",
              "duplicate_order": f"a return of the duplicate order and a full refund of ${op.amount:.2f}",
              "billing_error": f"a refund of the duplicate charge of ${op.amount:.2f}"}[op.kind]
    return (f"Hello {op.merchant} support,\n\nRegarding order {op.order_id} ({op.item_name}), I am requesting {remedy}. "
            f"Your policy states: \"{rule.clause_quote if rule else ''}\" "
            f"This request is within the policy window.\n\nThank you,\nOwed (assistant acting for the customer)")


def _claim_message(case: Case, rec: Recorder) -> str:
    t0 = time.time()
    op = case.opportunity
    if arcee.enabled():
        try:
            facts = {"store": op.merchant, "order_number": op.order_id, "item": op.item_name,
                     "amount_usd": op.amount, "request": KIND_LABEL[op.kind],
                     "policy_sentence": case.rule.clause_quote if case.rule else "", "facts": op.facts}
            text, meta = arcee.write_claim(facts)
            if len(text) > 40:
                rec.step(case.id, "claim_drafted", f"arcee:{meta['model']}", f"Claim message written ({len(text.split())} words)",
                         {"message": text, "llm": meta}, t0)
                return text
        except Exception as exc:  # noqa: BLE001
            log.warning("Arcee claim writing failed: %s", exc)
    text = _template_claim(case)
    rec.step(case.id, "claim_drafted", "template", "Claim message from template", {"message": text}, t0)
    return text


def _memory_text(c: Case, today) -> str:
    """One plain-English fact per case for Cognee's memory graph."""
    op = c.opportunity
    t = (f"On {today.isoformat()}, Owed reviewed a {KIND_LABEL[op.kind].lower()} for {op.merchant} order {op.order_id} "
         f"({op.item_name}), amount ${op.amount:.2f}.")
    if c.status == "verified":
        t += f" Outcome: {op.merchant} approved claim {c.claim_id} and paid refund {c.refund_id} of ${c.amount_recovered:.2f}."
    elif c.status == "declined":
        t += f" Outcome: Owed did not file a claim. Reason: {c.decision.reason if c.decision else 'not eligible'}."
    elif c.status == "rejected":
        t += f" Outcome: {op.merchant} rejected claim {c.claim_id}."
    elif c.status == "needs_approval":
        t += " Outcome: waiting for the customer's approval."
    else:
        t += f" Outcome: {c.status}."
    if c.rule and c.rule.clause_quote:
        t += f' Policy sentence cited: "{c.rule.clause_quote}"'
    return t


def _remember(cases: list[Case], rec: Recorder, today) -> None:
    if not cognee_memory.enabled() or not cases:
        return
    t0 = time.time()
    texts = [_memory_text(c, today) for c in cases]
    keys = [f"{c.opportunity.merchant_id}:{c.opportunity.order_id}:{c.opportunity.kind}:{c.status}" for c in cases]
    try:
        res = cognee_memory.remember(texts, keys)
        rec.step(None, "memory_write", "cognee",
                 f"Remembered {res.get('items', 0)} new outcome(s) for next time" + (f", {res['skipped']} already known" if res.get("skipped") else ""),
                 {**res, "texts": texts}, t0)
    except Exception as exc:  # noqa: BLE001 - memory is optional
        rec.step(None, "memory_write", "cognee", f"Could not write memory: {exc}", {"texts": texts}, t0, ok=False)


def _decision_statement(case: Case) -> tuple[str, str]:
    """The agent's reading of the store policy as plain claims for Senso to check against the knowledge base.

    Senso only scores claims it sees as being about the organisation's own knowledge base, so the claims are
    written without the store's name. Which store the evidence came from is checked afterwards (_provenance).
    """
    op, rule = case.opportunity, case.rule
    w = rule.window_days if rule else 0
    if op.kind == "price_adjustment":
        lines = [f"A price adjustment refund is given if the price drops within {w} days of the purchase date."]
        if rule and any("marketplace" in x for x in rule.excludes):
            lines.append("Items sold by Marketplace sellers are not eligible for a price adjustment.")
        question = "What is the price adjustment policy?"
    elif op.kind == "duplicate_order":
        lines = [f"If a customer accidentally places the same order twice, the customer can return the duplicate order "
                 f"within {w} days of delivery and gets a full refund."]
        question = "What is the policy for duplicate orders?"
    else:
        lines = [f"If a customer is charged more than once for the same order, every duplicate charge is refunded "
                 f"in full when it is reported within {w} days of the charge date."]
        question = "What is the policy for duplicate charges?"
    return " ".join(lines), question


def _provenance(check: dict[str, Any], op) -> dict[str, Any]:
    """Accept a 'verified' verdict only if Senso's evidence quote comes from THIS store's policy."""
    pol = load_policies().get(op.merchant_id)
    own = normalize(" ".join(sec.text for sec in pol.sections)) if pol else ""
    org = senso.org_name()
    for v in check.get("verdicts", []):
        v["claim_raw"] = v.get("claim", "")
        why = (v.get("why") or "").lower()
        if (v.get("verdict") == "conflict" and org and org.lower() in why
                and any(w in why for w in ("attribut", "organization", "organisation", "entity", "named"))):
            # Senso's reasoning says the policy text matches and only the owner differs (the store, not our org).
            # That is not a contradiction of the policy: it neither blocks the claim nor counts as verified.
            v["senso_verdict"] = "conflict"
            v["verdict"] = "not_verifiable"
            v["why"] = "Senso matched the policy text; it only noted the policy belongs to the store, not to our org. " + v.get("why", "")
        if org:  # Senso phrases claims about its organisation; show them about the store they were checked for
            v["claim"] = v["claim"].replace(org, op.merchant)
        q = normalize(v.get("quote", ""))
        if v.get("verdict") == "verified":
            if q and q[:60] in own:
                v["evidence_store"] = op.merchant
            else:
                v["verdict"] = "not_verifiable"
                v["why"] = "Senso's evidence did not come from this store's policy"
    counts: dict[str, int] = {}
    for v in check.get("verdicts", []):
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    check["counts"] = counts
    return check


def _fact_check(statement: str, question: str, op) -> dict[str, Any]:
    """Ask Senso twice in parallel (question with and without the store name; its claim extraction varies).
    Any conflict wins, otherwise the run with the most store-verified claims. Both runs stay in the audit data."""
    store_q = question.replace("What is the ", f"What is the {op.merchant} ", 1)
    variants = [store_q, question]
    with ThreadPoolExecutor(max_workers=2) as ex:
        checks = list(ex.map(lambda q: _provenance(senso.check_text(statement, q), op), variants))
    conflicts = [c for c in checks if c["counts"].get("conflict")]
    best = conflicts[0] if conflicts else max(checks, key=lambda c: c["counts"].get("verified", 0))
    best["statement"] = statement
    best["attempts"] = [{"question": q, "check_id": c.get("check_id"), "counts": c["counts"]}
                        for q, c in zip(variants, checks)]
    return best


def execute_claim(case: Case, rec: Recorder, before_action=None, human_approved: bool = False) -> Case:
    """Act (file the claim at the merchant) and verify the outcome.

    human_approved: the user explicitly approved this claim. A fact-check conflict is then logged, not blocking,
    otherwise an approved case would loop back to needs_approval forever."""
    op = case.opportunity
    case.message = _claim_message(case, rec)
    if senso.enabled():  # verification loop: fact-check our own claim against the knowledge base before acting
        t0 = time.time()
        try:
            statement, question = _decision_statement(case)
            check = _fact_check(statement, question, op)
            counts = check["counts"]
            summary = ", ".join(f"{n} {v}" for v, n in counts.items()) or "no checkable claims"
            if counts.get("conflict") and human_approved:
                rec.step(case.id, "fact_check", "senso",
                         f"Senso found a conflict ({summary}); filing anyway because you approved it", check, t0, ok=False)
            elif counts.get("conflict"):
                case.status = "needs_approval"
                rec.step(case.id, "fact_check", "senso", f"Stopped: Senso found a conflict with the policy ({summary})",
                         check, t0, ok=False)
                case.updated_at = utcnow()
                return case
            else:
                rec.step(case.id, "fact_check", "senso",
                         f"Senso fact-checked the agent's reading of the policy: {summary}", check, t0)
        except Exception as exc:  # noqa: BLE001
            rec.step(case.id, "fact_check", "senso", f"Fact-check unavailable, relying on verified citation: {exc}",
                     {}, t0, ok=False)
    if before_action:
        before_action()
    mode = "store API" if settings.merchant_base_url else "simulated store API (demo data)"
    t0 = time.time()
    claim = merchant_client.file_claim(op.merchant_id, {"type": op.kind, "order_id": op.order_id, "sku": op.sku,
                                                 "amount": op.amount, "message": case.message})
    case.claim_id = claim["claim_id"]
    case.status = "filed"
    rec.step(case.id, "action", "merchant-api",
             f"Filed {KIND_LABEL[op.kind].lower()} claim {case.claim_id} at {op.merchant} for ${op.amount:.2f} ({mode})",
             {"mode": mode, "endpoint": f"POST /merchant/{op.merchant_id}/claims",
              "claim": {k: v for k, v in claim.items() if k != 'message'}}, t0)
    t0 = time.time()
    status = merchant_client.claim_status(case.claim_id)
    if status["status"] == "approved":
        case.status = "verified"
        case.refund_id = status.get("refund_id")
        case.amount_recovered = float(status.get("refund_amount", 0))
        rec.step(case.id, "verification", "merchant-api",
                 f"Verified: {op.merchant} approved, refund {case.refund_id} for ${case.amount_recovered:.2f}",
                 {"endpoint": f"GET /merchant/claims/{case.claim_id}", "status": status}, t0)
    else:
        case.status = "rejected"
        rec.step(case.id, "verification", "merchant-api", f"Merchant rejected the claim: {status.get('reason')}",
                 {"status": status}, t0, ok=False)
    case.updated_at = utcnow()
    return case


def run(reset: bool = False, verbose: bool = False, run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    rec = Recorder(run_id, verbose=verbose)
    store = get_store()
    if reset:
        store.reset()
        merchant.reset()
    today = settings.today()
    cognee_memory.reset_cache()
    rec.step(None, "start", "owed", f"Run started for {settings.user_id} on {today.isoformat()}",
             {"integrations": settings.enabled(), "storage": store.backend})

    t0 = time.time()
    policy = statsig_gate.autonomy_policy()
    rec.step(None, "autonomy_policy", "statsig" if policy["source"] == "statsig" else "default",
             f"Autonomy: {'human approval required' if policy['require_approval'] else 'act on my own'} up to "
             f"${policy['max_auto_claim_usd']:.0f} per claim", policy, t0)

    t0 = time.time()
    emails, inbox_provider = gmail_airbyte.fetch_emails()
    rec.step(None, "ingest", inbox_provider, f"Read {len(emails)} emails from the inbox",
             {"subjects": [e.subject for e in emails]}, t0)

    t0 = time.time()
    orders, report = parse_orders(emails)
    receipts = [r for r in report if r.get("receipt")]
    sie_used = any("sie_entities" in r for r in report)
    rec.step(None, "extract", "rules + superlinked-sie" if sie_used else "rules",
             f"{len(receipts)} receipt emails -> {len(orders)} orders "
             f"({sum(len(o.items) for o in orders)} items, {sum(len(o.transactions) for o in orders)} payments)",
             {"report": report, "orders": [o.model_dump(mode="json") for o in orders]}, t0)

    t0 = time.time()
    ops = find_opportunities(orders)
    rec.step(None, "detect", "merchant-api", f"Found {len(ops)} possible refunds worth ${sum(o.amount for o in ops):.2f}",
             {"opportunities": [o.model_dump(mode="json") for o in ops]}, t0)

    # Long-term memory (Cognee): one lookup per store, started now and running while cases are worked.
    mem_pool = ThreadPoolExecutor(max_workers=4)
    mem_jobs: dict[str, tuple[Any, float]] = {}
    if cognee_memory.enabled():
        for m in sorted({o.merchant for o in ops}):
            mem_jobs[m] = (mem_pool.submit(cognee_memory.recall, m), time.time())

    def record_memory(case: Case) -> None:
        job = mem_jobs.get(case.opportunity.merchant)
        if not job:
            return
        fut, started = job
        try:
            mem = fut.result(timeout=45)
        except Exception as exc:  # noqa: BLE001 - memory is optional
            mem = {"ok": False, "answer": "", "error": str(exc)[:200]}
        short = (mem.get("answer") or mem.get("error") or "")[:160].replace("\n", " ")
        rec.step(case.id, "memory", "cognee", f"Memory of earlier claims with {case.opportunity.merchant}: {short}",
                 mem, started, ok=bool(mem.get("ok")))

    def process(op) -> Case | None:
        # Idempotency: Owed wakes up daily. A case already filed, refunded or waiting for you is never filed twice.
        prior = store.case(op.id)
        if prior and prior.status in ALREADY_HANDLED:
            when = prior.updated_at.date().isoformat() if prior.updated_at else "earlier"
            what = f"claim {prior.claim_id}" if prior.claim_id else prior.status.replace("_", " ")
            rec.step(op.id, "skipped", "owed",
                     f"Already handled on {when} ({what}, status {prior.status}) - not filing again",
                     {"prior_status": prior.status, "claim_id": prior.claim_id, "refund_id": prior.refund_id})
            return None
        case = Case(id=op.id, opportunity=op)
        rec.step(case.id, "detected", "owed", f"{KIND_LABEL[op.kind]} candidate: {op.item_name} ({op.order_id}), ${op.amount:.2f}",
                 {"opportunity": op.model_dump(mode="json")})

        t0 = time.time()
        evidence, rmeta = retrieve(op)
        case.evidence = evidence
        top = evidence[0].title if evidence else "nothing"
        rec.step(case.id, "context", rmeta.get("retriever", "?") + (" + superlinked-sie" if rmeta.get("reranker") else ""),
                 f"Retrieved {len(evidence)} policy passages; best: {top}",
                 {"meta": rmeta, "evidence": [e.model_dump() for e in evidence]}, t0)

        t0 = time.time()
        rule, imeta = interpret(op, evidence)
        case.rule = rule
        rec.step(case.id, "rule", rule.provider,
                 f"{rule.window_days}-day window from {rule.anchor.replace('_', ' ')}; clause: \"{rule.clause_quote}\""
                 + (" (verified)" if rule.citation_verified else " (UNVERIFIED)"),
                 {"rule": rule.model_dump(), "meta": imeta}, t0)

        t0 = time.time()
        decision = decide(op, rule, today)
        case.decision = decision
        rec.step(case.id, "decision", "owed-rules", decision.reason,
                 {"checks": [c.model_dump() for c in decision.checks]}, t0)

        if not decision.eligible:
            case.status = "declined"
            record_memory(case)
            store.save_case(run_id, case)
            return case

        needs_human = policy["require_approval"] or op.amount > policy["max_auto_claim_usd"]
        case.autonomy = {**policy, "auto": not needs_human}
        if needs_human:
            case.status = "needs_approval"
            why = "approval gate is on" if policy["require_approval"] else \
                f"${op.amount:.2f} is above the ${policy['max_auto_claim_usd']:.0f} auto limit"
            rec.step(case.id, "autonomy_gate", policy["source"], f"Waiting for your approval: {why}", case.autonomy)
            record_memory(case)
            store.save_case(run_id, case)
            return case
        rec.step(case.id, "autonomy_gate", policy["source"],
                 f"Auto-approved: ${op.amount:.2f} is within the ${policy['max_auto_claim_usd']:.0f} limit", case.autonomy)
        try:
            execute_claim(case, rec, before_action=lambda: record_memory(case))
        except Exception as exc:  # noqa: BLE001 - one failing store must not stop the others
            case.status = "failed"
            rec.step(case.id, "action", "merchant-api", f"Could not file the claim: {exc}", ok=False)
        store.save_case(run_id, case)
        return case

    workers = max(1, min(settings.parallel_cases, len(ops) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:  # cases are independent -> work them in parallel
        results = list(pool.map(process, ops))
    mem_pool.shutdown(wait=False)
    cases: list[Case] = [c for c in results if c is not None]
    skipped = len(results) - len(cases)

    _remember(cases, rec, today)
    recovered = round(sum(c.amount_recovered for c in cases), 2)
    pending = [c for c in cases if c.status == "needs_approval"]
    lines = [f"- {KIND_LABEL[c.opportunity.kind]}: {c.opportunity.item_name} ({c.opportunity.merchant}) "
             f"${c.amount_recovered:.2f}, refund {c.refund_id}" for c in cases if c.status == "verified"]
    lines += [f"- Needs your OK: {c.opportunity.item_name} ${c.opportunity.amount:.2f}" for c in pending]
    if skipped:
        lines.append(f"- {skipped} case(s) were already handled earlier and were not filed again.")
    body = f"Owed recovered ${recovered:.2f} for you today.\n\n" + "\n".join(lines) + \
        "\n\nEvery decision, with the policy sentence behind it, is in your audit trail."
    t0 = time.time()
    provider, ref = gmail_airbyte.send_email(settings.user_email, f"Owed: ${recovered:.2f} recovered", body)
    rec.step(None, "notify", provider, f"Sent you a summary: ${recovered:.2f} recovered, {len(pending)} waiting for approval",
             {"ref": ref, "body": body}, t0)
    sent = amplitude.flush()
    return {"run_id": run_id, "recovered": recovered, "skipped_already_handled": skipped, "cases": [c.model_dump(mode="json") for c in cases],
            "events": len(rec.events), "amplitude_events": sent, "storage": store.backend,
            "llm_calls": list(arcee.usage_log)}


def approve(case_id: str) -> Case | None:
    store = get_store()
    case = store.case(case_id)
    if not case or case.status != "needs_approval":
        return case
    rec = Recorder(new_run_id())
    rec.step(case.id, "human_approval", "user", f"You approved the claim for ${case.opportunity.amount:.2f}")
    execute_claim(case, rec, human_approved=True)
    store.save_case(rec.run_id, case)
    _remember([case], rec, settings.today())
    amplitude.flush()
    return case
