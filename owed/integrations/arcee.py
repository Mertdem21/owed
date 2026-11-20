"""Arcee: open-weight Trinity models through an OpenAI-compatible API (https://api.arcee.ai/api/v1).

Pareto routing - the cheapest model that is good enough for each step:
  small model  -> read a policy clause and turn it into rule parameters (JSON)
  large model  -> write the claim message to the merchant
Everything else (dates, amounts, eligibility) is plain Python, not an LLM.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..config import settings

log = logging.getLogger("owed.arcee")
_client: Any = None
_models: dict[str, str] = {}
usage_log: list[dict[str, Any]] = []


def enabled() -> bool:
    return bool(settings.arcee_api_key)


def client() -> Any:
    global _client
    if _client is not None:
        return _client
    kwargs = {"api_key": settings.arcee_api_key, "base_url": settings.arcee_base_url, "timeout": 60.0}
    if settings.amplitude_api_key:
        try:  # Amplitude Agent Analytics wraps the OpenAI client and tracks every LLM call
            from amplitude import Amplitude  # type: ignore
            from amplitude_ai import AmplitudeAI, OpenAI as AmpOpenAI  # type: ignore

            ai = AmplitudeAI(amplitude=Amplitude(settings.amplitude_api_key))
            _client = AmpOpenAI(amplitude=ai, **kwargs)
            return _client
        except Exception as exc:  # noqa: BLE001
            log.info("amplitude-ai wrapper not used: %s", exc)
    from openai import OpenAI

    _client = OpenAI(**kwargs)
    return _client


SMALL_CANDIDATES = ["trinity-mini", "arcee-ai/trinity-mini", "trinity-mini-thinking", "arcee-ai/trinity-mini-thinking",
                    "trinity-nano", "arcee-ai/trinity-nano"]
LARGE_CANDIDATES = ["trinity-large-thinking", "arcee-ai/trinity-large-thinking", "trinity-large-preview",
                    "arcee-ai/trinity-large-preview", "trinity-large", "arcee-ai/trinity-large"]
_working: dict[str, str] = {}
_errors: list[str] = []


def models() -> dict[str, str]:
    """Candidate model ids per tier: .env override first, then ids from the live model list, then known names."""
    if _models:
        return _models
    available: list[str] = []
    try:
        available = [m.id for m in client().models.list().data]
    except Exception as exc:  # noqa: BLE001
        _errors.append(f"model list: {exc}")
        log.warning("could not list Arcee models: %s", exc)

    def order(preferred: str, needles: list[str], known: list[str]) -> list[str]:
        out = [preferred] if preferred else []
        out += [m for n in needles for m in available if n in m.lower()]
        out += [m for m in available] if available else known  # only ids this key can actually use
        return list(dict.fromkeys([m for m in out if m]))

    # Pareto routing: a fast/cheap open model for reading rules, a stronger open model for writing claims
    _models["small_candidates"] = order(settings.arcee_model_small,
                                        ["trinity-mini", "trinity-nano", "deepseek-v4.1-flash", "flash", "mini"],
                                        SMALL_CANDIDATES)
    _models["large_candidates"] = order(settings.arcee_model_large,
                                        ["trinity-large", "kimi-k3.1-pro", "glm-5.3", "kimi", "pro", "large"],
                                        LARGE_CANDIDATES)
    _models["small"] = _models["small_candidates"][0]
    _models["large"] = _models["large_candidates"][0]
    _models["available"] = ", ".join(available) or "(model list unavailable)"
    return _models


def _clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return text.strip()


def chat(tier: str, system: str, user: str, max_tokens: int = 700, json_mode: bool = False) -> tuple[str, dict]:
    m = models()
    candidates = [_working[tier]] if tier in _working else m[f"{tier}_candidates"]
    if tier == "large" and tier not in _working:  # if no large model is accessible, fall back to the small tier
        candidates = candidates + [c for c in m["small_candidates"] if c not in candidates]
    last_exc: Exception | None = None
    for model in candidates:
        t0 = time.time()
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "temperature": 0.1,
                                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            try:
                resp = client().chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - retry once without json mode (not every model supports it)
                if not json_mode or _is_model_error(exc):
                    raise
                kwargs.pop("response_format", None)
                resp = client().chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if _is_model_error(exc):
                _errors.append(f"{model}: {str(exc)[:120]}")
                continue
            raise
        _working[tier] = model
        text = _clean(resp.choices[0].message.content or "")
        usage = getattr(resp, "usage", None)
        meta = {"model": model, "tier": tier, "latency_ms": int((time.time() - t0) * 1000),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None)}
        usage_log.append(meta)
        return text, meta
    raise RuntimeError(f"no accessible Arcee model for tier {tier}: {last_exc}")


def _is_model_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("404", "not accessible", "model_not_found", "does not exist", "unknown model",
                                  "no such model", "invalid model"))


def parse_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    return json.loads(m.group(0))


RULE_SYSTEM = (
    "You read a store policy and extract ONE rule as JSON. Use only the given passages. "
    'Return exactly: {"applies": true|false, "window_days": int|null, '
    '"anchor": "purchase_date"|"delivery_date"|"charge_date", "excludes": [short strings], '
    '"clause_quote": "the single sentence from the passages that grants the refund, copied verbatim"}'
)


def interpret_rule(kind: str, merchant: str, passages: list[str]) -> tuple[dict[str, Any], dict]:
    question = {
        "price_adjustment": "Does the store refund the difference when it lowers the price after purchase? Within how many days, counted from what date? What is excluded?",
        "duplicate_order": "Can a duplicate (accidentally repeated) order be returned for a full refund? Within how many days, counted from what date?",
        "billing_error": "Does the store refund a duplicate charge for the same order? Within how many days of the charge must it be reported?",
    }[kind]
    user = f"Store: {merchant}\nQuestion: {question}\n\nPassages:\n" + "\n---\n".join(passages)
    text, meta = chat("small", RULE_SYSTEM, user, max_tokens=900, json_mode=True)
    return parse_json(text), meta


CLAIM_SYSTEM = (
    "You write short, polite, firm customer claims on behalf of a shopper. Plain text, max 120 words. "
    "Cite the store policy sentence in quotes. Include order number, item, amount and the requested remedy. "
    "No placeholders, no subject line, sign as 'Owed (assistant acting for the customer)'."
)


def write_claim(facts: dict[str, Any]) -> tuple[str, dict]:
    return chat("large", CLAIM_SYSTEM, json.dumps(facts, default=str), max_tokens=1200)


def smoke() -> str:
    if not enabled():
        return "SKIP: ARCEE_API_KEY not set"
    m = models()
    try:
        text, meta = chat("small", "Answer with one word.", "Say OK.", max_tokens=200)
        return (f"OK: small={meta['model']} reply={text[:40]!r} ({meta['latency_ms']} ms)\n"
                f"   available: {m['available']}")
    except Exception as exc:  # noqa: BLE001
        return (f"FAIL: {str(exc)[:200]}\n   available: {m['available']}\n   tried: "
                + " | ".join(_errors[-8:]))
