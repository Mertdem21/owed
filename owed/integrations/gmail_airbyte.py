"""Airbyte Gmail agent connector: reads receipt emails from the inbox and sends the user a summary.

    uv pip install airbyte-agent-gmail     (included in `uv sync --extra sponsors`)
    connector.execute(entity, action, params)   e.g. ("messages", "list", {...})

Falls back to data/inbox/demo_inbox.json (and data/runtime/outbox/) when Gmail is not configured.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from email.mime.text import MIMEText
from typing import Any

from ..config import DATA, RUNTIME, settings
from ..models import Email

log = logging.getLogger("owed.gmail")
QUERY = "subject:(order OR receipt OR payment OR delivered) newer_than:90d"
OUTBOX = RUNTIME / "outbox"


def enabled() -> bool:
    return settings.enabled()["airbyte_gmail"] and settings.inbox_source != "local"


def _connector() -> Any:
    from airbyte_agent_gmail import GmailConnector  # type: ignore
    from airbyte_agent_gmail.models import GmailAuthConfig  # type: ignore

    return GmailConnector(auth_config=GmailAuthConfig(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        refresh_token=settings.google_refresh_token,
    ))


def _run(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except RuntimeError:  # already inside an event loop
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _as_dict(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__") and not isinstance(obj, dict):
        return dict(obj.__dict__)
    return obj


def _rows(result: Any) -> list[dict[str, Any]]:
    result = _as_dict(result)
    if isinstance(result, list):
        return [_as_dict(r) for r in result]
    if isinstance(result, dict):
        for key in ("data", "messages", "records", "items", "result"):
            if isinstance(result.get(key), list):
                return [_as_dict(r) for r in result[key]]
    return []


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _body(payload: dict[str, Any]) -> str:
    if not payload:
        return ""
    if payload.get("mimeType", "").startswith("text/plain") and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        text = _body(part)
        if text:
            return text
    if payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    return ""


def _to_email(msg: dict[str, Any]) -> Email:
    payload = msg.get("payload") or {}
    headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", []) or []}
    body = _body(payload) or msg.get("snippet", "") or msg.get("body", "")
    return Email(id=str(msg.get("id")), sender=headers.get("from", msg.get("from", "")),
                 subject=headers.get("subject", msg.get("subject", "")),
                 date=headers.get("date", msg.get("date", "")), body=body)


async def _fetch_gmail(limit: int) -> list[Email]:
    conn = _connector()
    listed = await conn.execute("messages", "list", {"q": QUERY, "max_results": limit})
    out = []
    for row in _rows(listed)[:limit]:
        full = await conn.execute("messages", "get", {"id": row.get("id"), "format": "full"})
        out.append(_to_email(_as_dict(full)))
    return out


def local_inboxes() -> list[str]:
    """Names of the local inboxes in data/inbox (demo, test, ...)."""
    return sorted(p.stem.removesuffix("_inbox") for p in (DATA / "inbox").glob("*_inbox.json"))


def load_local_inbox(name: str = "demo") -> list[Email]:
    if name not in local_inboxes():
        raise ValueError(f"unknown inbox '{name}' - available: {', '.join(local_inboxes())}")
    rows = json.loads((DATA / "inbox" / f"{name}_inbox.json").read_text())
    return [Email(id=r["id"], sender=r["from"], subject=r["subject"], date=r["date"], body=r["body"]) for r in rows]


def fetch_emails(limit: int = 50, inbox: str | None = None) -> tuple[list[Email], str]:
    """Returns (emails, provider). An explicit local inbox (e.g. 'test') never touches Gmail."""
    if inbox:
        return load_local_inbox(inbox), f"{inbox}-inbox"
    if enabled():
        try:
            emails = _run(_fetch_gmail(limit))
            if emails:
                return emails, "airbyte-gmail"
            log.warning("Gmail returned no receipt emails, using demo inbox")
        except Exception as exc:  # noqa: BLE001
            log.warning("Airbyte Gmail failed, using demo inbox: %s", exc)
    return load_local_inbox(), "demo-inbox"


async def _send_gmail(raw: str) -> Any:
    conn = _connector()
    last_exc: Exception | None = None
    for entity, action in (("messages", "send"), ("messages", "create"), ("drafts_send", "create")):
        try:
            return await conn.execute(entity, action, {"raw": raw})
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise RuntimeError(f"could not send via connector: {last_exc}")


def send_email(to: str, subject: str, body: str) -> tuple[str, str]:
    """Returns (provider, reference). Writes to the local outbox when Gmail is unavailable."""
    msg = MIMEText(body)
    msg["to"], msg["subject"] = to, subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    if enabled() and to:
        try:
            res = _as_dict(_run(_send_gmail(raw)))
            return "airbyte-gmail", str((res or {}).get("id", "sent")) if isinstance(res, dict) else "sent"
        except Exception as exc:  # noqa: BLE001
            log.warning("Gmail send failed, writing to outbox: %s", exc)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    path = OUTBOX / f"{subject[:40].replace(' ', '_').replace('/', '-')}.txt"
    path.write_text(f"To: {to or settings.user_id}\nSubject: {subject}\n\n{body}")
    return "local-outbox", str(path.relative_to(DATA.parent))


def smoke() -> str:
    if not settings.enabled()["airbyte_gmail"]:
        return "SKIP: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN not set"
    try:
        emails = _run(_fetch_gmail(5))
        return f"OK: {len(emails)} matching email(s): {[e.subject[:40] for e in emails]}"
    except Exception as exc:  # noqa: BLE001
        return f"FAIL: {exc}"
