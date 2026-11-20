"""Put the 12 demo emails into your own Gmail INBOX (IMAP APPEND with your app password). Nothing is sent.

Each message keeps its fictional store as sender and gets a footer "Owed demo data", which GMAIL_QUERY matches,
so Owed reads exactly these messages from Gmail and nothing else from your inbox.

    uv run python -m scripts.seed_gmail          (or double-click seed_gmail.command)
"""
from __future__ import annotations

import imaplib
import json
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from owed.config import DATA, settings

FOOTER = "\n\n--\nOwed demo data (fictional store, used for the Owed hackathon demo)"
NAMES = {"northwind-electronics": "Northwind Electronics", "cascade-outfitters": "Cascade Outfitters",
         "brightline-home": "Brightline Home"}


def display_name(addr: str) -> str:
    for key, name in NAMES.items():
        if key in addr:
            return name
    return addr.split("@")[0].capitalize()


def main() -> None:
    if not (settings.gmail_address and settings.gmail_app_password):
        raise SystemExit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env first.")
    rows = json.loads((DATA / "inbox" / "demo_inbox.json").read_text())
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    conn.login(settings.gmail_address, settings.gmail_app_password.replace(" ", ""))
    conn.select("INBOX", readonly=True)
    typ, data = conn.uid("search", None, "X-GM-RAW", '"Owed demo data"')
    have = len((data[0] or b"").split()) if typ == "OK" else 0
    if have >= len(rows):
        print(f"Already in your inbox: {have} demo messages. Nothing added.")
        conn.logout()
        return
    now = time.time()
    for i, r in enumerate(rows):
        ts = now - (len(rows) - i) * 45  # keep the original order, newest last
        m = EmailMessage()
        m["From"] = f"{display_name(r['from'])} <{r['from']}>"
        m["To"] = settings.gmail_address
        m["Subject"] = r["subject"]
        m["Date"] = formatdate(ts, localtime=True)
        m["Message-ID"] = make_msgid(domain="owed-demo.local")
        m.set_content(r["body"] + FOOTER)
        typ, resp = conn.append("INBOX", "", imaplib.Time2Internaldate(ts), m.as_bytes())
        print(f"{'ok ' if typ == 'OK' else 'ERR'} {r['subject']}")
    conn.logout()
    print(f"Done: {len(rows)} demo messages are in {settings.gmail_address}'s inbox.")


if __name__ == "__main__":
    main()
