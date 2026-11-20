"""Tiny HTTP helper so the core never hard-depends on httpx at import time."""
from __future__ import annotations

from typing import Any


class IntegrationError(RuntimeError):
    pass


def request(method: str, url: str, *, json: Any = None, headers: dict[str, str] | None = None,
            params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise IntegrationError("httpx is not installed - run `uv sync`") from exc
    try:
        resp = httpx.request(method, url, json=json, headers=headers, params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        raise IntegrationError(f"{method} {url} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise IntegrationError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"text": resp.text}
