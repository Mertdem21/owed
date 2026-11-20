"""Local policy registry: the merchant policy documents in data/policies.

They are the ground truth that gets uploaded to Senso (scripts/seed_senso.py). The front-matter holds the
machine-readable rule parameters, used as an independent second reading next to the LLM interpretation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

from .config import DATA

KIND_KEYWORDS = {
    "price_adjustment": "price adjustment lowers the price refund the difference within days of purchase",
    "duplicate_order": "duplicate order placed the same order twice return the duplicate within days of delivery",
    "billing_error": "charged more than once duplicate charge refund billing error within days",
}


@dataclass
class Section:
    merchant_id: str
    merchant: str
    heading: str
    text: str

    @property
    def title(self) -> str:
        return f"{self.merchant} - {self.heading}"

    @property
    def source_id(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.heading.lower()).strip("-")
        return f"policy:{self.merchant_id}#{slug}"


@dataclass
class Policy:
    merchant_id: str
    merchant: str
    rules: dict[str, Any]
    sections: list[Section] = field(default_factory=list)
    file: str = ""


@lru_cache(maxsize=1)
def load_policies() -> dict[str, Policy]:
    out: dict[str, Policy] = {}
    for path in sorted((DATA / "policies").glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta: dict[str, Any] = {}
        body = raw
        if raw.startswith("---"):
            _, fm, body = raw.split("---", 2)
            meta = yaml.safe_load(fm) or {}
        merchant_id = meta.get("merchant_id", path.stem)
        merchant = meta.get("merchant", path.stem)
        policy = Policy(merchant_id, merchant, meta.get("rules", {}), file=path.name)
        current, buf = None, []
        for line in body.splitlines():
            if line.startswith("## "):
                if current:
                    policy.sections.append(Section(merchant_id, merchant, current, "\n".join(buf).strip()))
                current, buf = line[3:].strip(), []
            elif current:
                buf.append(line)
        if current:
            policy.sections.append(Section(merchant_id, merchant, current, "\n".join(buf).strip()))
        out[merchant_id] = policy
    return out


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def local_search(merchant_id: str, query: str, k: int = 3) -> list[tuple[Section, float]]:
    policy = load_policies().get(merchant_id)
    if not policy:
        return []
    q = _tokens(query)
    scored = []
    for s in policy.sections:
        toks = _tokens(s.heading + " " + s.text)
        score = len(q & toks) / (len(q) or 1)
        scored.append((s, round(score, 3)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():  # a heading line is its own unit, never glued to the next sentence
        out += [p.strip() for p in re.split(r"(?<=[.!?])\s+", line) if p.strip()]
    return out


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$%. ]", " ", text.lower())).strip()
