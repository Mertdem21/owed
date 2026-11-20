"""How the agent talks to a store. In-process mock by default; set MERCHANT_BASE_URL to call a store API over HTTP
(e.g. this app's /merchant endpoints on another host, or a Firedrill synthetic store)."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from . import merchant
from .config import settings
from .integrations._http import IntegrationError, request


def _remote() -> str:
    return settings.merchant_base_url.rstrip("/")


def current_price(merchant_id: str, sku: str) -> dict[str, Any] | None:
    if not _remote():
        return merchant.current_price(merchant_id, sku)
    try:
        return request("GET", f"{_remote()}/merchant/{merchant_id}/products/{quote(sku)}")
    except IntegrationError:
        return None


def file_claim(merchant_id: str, claim: dict[str, Any]) -> dict[str, Any]:
    if not _remote():
        return merchant.file_claim(merchant_id, claim)
    return request("POST", f"{_remote()}/merchant/{merchant_id}/claims", json=claim)


def claim_status(claim_id: str) -> dict[str, Any]:
    if not _remote():
        return merchant.claim_status(claim_id)
    return request("GET", f"{_remote()}/merchant/claims/{quote(claim_id)}")
