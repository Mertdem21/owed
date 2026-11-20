"""Move the plain-text demo emails added by scripts.seed_gmail (footer "Owed demo data") to Gmail's trash."""
from __future__ import annotations

import imaplib

from owed.config import settings


def main() -> None:
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    conn.login(settings.gmail_address, settings.gmail_app_password.replace(" ", ""))
    conn.select("INBOX")
    typ, data = conn.uid("search", None, "X-GM-RAW", '"Owed demo data (fictional store"')
    uids = (data[0] or b"").split() if typ == "OK" else []
    for uid in uids:
        conn.uid("store", uid, "+X-GM-LABELS", "\\Trash")
    conn.logout()
    print(f"Moved {len(uids)} old plain-text demo email(s) to Trash.")


if __name__ == "__main__":
    main()
