"""Gmail over IMAP with an app password: the quickest way to read a real inbox (no Google Cloud project).

- The mailbox is opened READ-ONLY (EXAMINE) and messages are fetched with BODY.PEEK, so nothing is marked read.
- Only messages matching GMAIL_QUERY are fetched (Gmail search syntax via X-GM-RAW). The default is Gmail's own
  Purchases category of the last 60 days; everything else in the inbox is never fetched.
- Any failure returns None and the agent falls back to the next inbox source.

Env: GMAIL_ADDRESS, GMAIL_APP_PASSWORD (16 characters from myaccount.google.com/apppasswords), GMAIL_QUERY (optional).
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape

from ..config import settings
from ..models import Email

log = logging.getLogger("owed.gmail")
last_error = ""


def enabled() -> bool:
    return bool(settings.gmail_address and settings.gmail_app_password)


def _text(value: str | None) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:  # noqa: BLE001
        return value or ""


def _body(msg: email.message.Message) -> str:
    plain, html = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (html if part.get_content_type() == "text/html" else plain).append(text)
    if plain:
        return "\n".join(plain).replace("\r\n", "\n").replace("\r", "\n")
    raw = "\n".join(html)
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
    return unescape(re.sub(r"<[^>]+>", " ", raw)).replace("\r\n", "\n").replace("\r", "\n")


def fetch(limit: int = 40) -> list[Email] | None:
    """Messages matching GMAIL_QUERY, oldest first. None when not configured or on any error."""
    global last_error
    if not enabled():
        return None
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
        conn.login(settings.gmail_address, settings.gmail_app_password.replace(" ", ""))
        try:
            conn.select("INBOX", readonly=True)
            query = settings.gmail_query.replace('"', "")
            typ, data = conn.uid("search", None, "X-GM-RAW", f'"{query}"')
            if typ != "OK":
                raise RuntimeError(f"search failed: {data}")
            uids = (data[0] or b"").split()[-limit:]
            out: list[Email] = []
            for uid in uids:
                typ, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in msg_data or [] if isinstance(p, tuple)), None)
                if typ != "OK" or not raw:
                    continue
                msg = email.message_from_bytes(raw)
                try:
                    date = parsedate_to_datetime(msg.get("Date")).isoformat()
                except Exception:  # noqa: BLE001
                    date = ""
                name, addr = parseaddr(_text(msg.get("From")))
                out.append(Email(id=f"gmail-{uid.decode()}", sender=addr or name, subject=_text(msg.get("Subject")),
                                 date=date, body=_body(msg)))
            out.sort(key=lambda e: e.date)
            last_error = ""
            return out
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        last_error = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("Gmail IMAP failed: %s", last_error)
        return None


def smoke() -> str:
    if not enabled():
        return "SKIP: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set"
    mails = fetch(limit=20)
    if mails is None:
        return f"FAIL: {last_error}"
    return f"OK: {len(mails)} message(s) match '{settings.gmail_query}': {[m.subject[:40] for m in mails[:4]]}"
