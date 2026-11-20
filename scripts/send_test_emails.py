"""Put the demo and/or test emails into a real Gmail inbox, so Owed reads them over IMAP like real receipts.

Default mode "append" (recommended): the emails are placed straight into the INBOX over IMAP with their original
sender (e.g. orders@northwind-electronics.example), original date and the Gmail label "owed". Nothing is sent over
the internet, no spam filter, and the inbox looks like a real one.
Mode "send": the emails are really sent over SMTP to GMAIL_ADDRESS (the sender is then your own address).

Safe to run twice: every email has a fixed Message-ID and is skipped if it is already in the inbox.

Usage:
    uv run python -m scripts.send_test_emails --inbox test            # 17 edge-case emails
    uv run python -m scripts.send_test_emails --inbox demo            # the 12 demo emails ($263.50)
    uv run python -m scripts.send_test_emails --inbox all             # both ($308.50 + 1 waiting for approval)
    uv run python -m scripts.send_test_emails --inbox test --dry-run  # show what would happen
    uv run python -m scripts.send_test_emails --remove                # delete every email this script added

Needs GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env (the same ones Owed uses to read the inbox).
Use a separate test Gmail account, never a personal one: the Owed web app is public.
"""
from __future__ import annotations

import argparse
import imaplib
import json
import re
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr

from owed.config import ROOT, settings
from owed.policies import load_policies

LABEL = "owed"
RECOMMENDED_QUERY = ("{label:owed subject:order subject:receipt subject:payment subject:delivered} "
                     "newer_than:120d")


def load(inbox: str) -> list[dict]:
    names = ["demo", "test"] if inbox == "all" else [inbox]
    rows: list[dict] = []
    for n in names:
        path = ROOT / "data" / "inbox" / f"{n}_inbox.json"
        if not path.exists():
            sys.exit(f"Unknown inbox '{n}' ({path} not found)")
        rows += [{**r, "_inbox": n} for r in json.loads(path.read_text())]
    return rows


def store_name(sender: str) -> str:
    domain = sender.split("@")[-1].split(".")[0].lower()
    for pol in load_policies().values():
        if pol.merchant.lower().replace(" ", "-") == domain:
            return pol.merchant
    return domain.replace("-", " ").title()


def message_id(row: dict) -> str:
    return f"<{row['_inbox']}-{row['id']}@owed.test>"


def build(row: dict, mode: str) -> EmailMessage:
    msg = EmailMessage()
    if mode == "append":
        msg["From"] = formataddr((store_name(row["from"]), row["from"]))
    else:  # Gmail rewrites the sender to your own address when sending over SMTP
        msg["From"] = formataddr((store_name(row["from"]), settings.gmail_address))
        msg["Reply-To"] = row["from"]
    msg["To"] = settings.gmail_address
    msg["Subject"] = row["subject"]
    msg["Date"] = format_datetime(datetime.fromisoformat(row["date"].replace("Z", "+00:00")))
    msg["Message-ID"] = message_id(row)
    msg["X-Owed-Test"] = row["_inbox"]
    msg.set_content(row["body"] + "\n")  # plain text: Owed reads lines like "Order number:" and "SKU:" exactly
    msg.add_alternative(html(row), subtype="html")  # what you see in Gmail: a normal store email
    return msg


# --- HTML version: looks like a real store email. Stores are fictional; products are real. --------------------------
BRAND = {"Northwind Electronics": ("#1d4ed8", "NORTHWIND", "ELECTRONICS"),
         "Cascade Outfitters": ("#2f6b4f", "CASCADE", "OUTFITTERS"),
         "Brightline Home": ("#b45309", "BRIGHTLINE", "HOME")}
SHIP_TO = "Alex Kim<br>San Francisco, CA"


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _field(body: str, name: str) -> str:
    m = re.search(rf"{name}:\s*(.+)", body)
    return m.group(1).strip() if m else ""


def _items(body: str) -> list[dict]:
    rx = re.compile(r"Item:\s*(.+?)\n\s*SKU:\s*(\S+)\n(?:\s*Sold by:\s*(.+?)\n)?\s*Qty:\s*(\d+)\n\s*Price:\s*(\$[\d,.]+)")
    return [dict(name=m[1], sku=m[2], seller=m[3] or "", qty=m[4], price=m[5]) for m in rx.finditer(body + "\n")]


def html(row: dict) -> str:
    body, subject = row["body"], row["subject"]
    store = store_name(row["from"])
    if store not in BRAND:  # unknown sender (e.g. the phishing test): plain, unbranded, like the real thing
        return ("<div style='font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:560px'>"
                + "".join(f"<p>{_esc(line)}</p>" for line in body.split("\n") if line.strip()) + "</div>")
    color, word1, word2 = BRAND[store]
    head = (f"<div style='background:{color};padding:18px 24px;color:#fff;font-family:Helvetica,Arial,sans-serif'>"
            f"<span style='font-size:20px;font-weight:800;letter-spacing:3px'>{word1}</span>"
            f"<span style='font-size:11px;letter-spacing:3px;margin-left:6px;opacity:.85'>{word2}</span></div>")
    order, items = _field(body, "Order number"), _items(body)
    s = subject.lower()
    if items:
        title, intro = "Thanks for your order!", f"We've received your order and will email you when it ships."
        rows = "".join(
            f"<tr><td style='padding:12px 0;border-bottom:1px solid #eee'><b>{_esc(i['name'])}</b><br>"
            f"<span style='color:#777;font-size:12px'>SKU {_esc(i['sku'])}"
            + (f" · Sold by {_esc(i['seller'])}" if i["seller"] and i["seller"] != store else "")
            + f" · Qty {i['qty']}</span></td>"
            f"<td style='padding:12px 0;border-bottom:1px solid #eee;text-align:right;vertical-align:top'>{i['price']}</td></tr>"
            for i in items)
        detail = (f"<table style='width:100%;border-collapse:collapse;font-size:14px'>{rows}"
                  f"<tr><td style='padding:12px 0'><b>Order total</b></td>"
                  f"<td style='padding:12px 0;text-align:right'><b>${_esc(_field(body, 'Order total').lstrip('$'))}</b></td></tr></table>"
                  f"<table style='width:100%;font-size:13px;color:#555;margin-top:8px'><tr>"
                  f"<td style='vertical-align:top'><b>Ship to</b><br>{SHIP_TO}</td>"
                  f"<td style='vertical-align:top'><b>Payment</b><br>{_esc(_field(body, 'Payment'))}</td></tr></table>")
        meta = f"Order <b>{_esc(order)}</b> · placed {_esc(_field(body, 'Order date'))}"
    elif "payment" in s:
        title, intro = "Payment receipt", "We've charged your card for the order below."
        detail = (f"<table style='width:100%;font-size:14px'>"
                  f"<tr><td>Transaction ID</td><td style='text-align:right'>{_esc(_field(body, 'Transaction ID'))}</td></tr>"
                  f"<tr><td>Charged on</td><td style='text-align:right'>{_esc(_field(body, 'Charged on'))}</td></tr>"
                  f"<tr><td>Payment</td><td style='text-align:right'>{_esc(_field(body, 'Payment'))}</td></tr>"
                  f"<tr><td style='padding-top:8px'><b>Amount</b></td><td style='text-align:right;padding-top:8px'>"
                  f"<b>{_esc(_field(body, 'Amount'))}</b></td></tr></table>")
        meta = f"Order <b>{_esc(order)}</b>"
    elif "deliver" in s:
        title, intro = "Your package was delivered", "Your order has arrived. We hope you love it."
        detail = f"<p style='font-size:14px'>Delivered on <b>{_esc(_field(body, 'Delivered on'))}</b></p>"
        meta = f"Order <b>{_esc(order)}</b>"
    else:  # newsletter and similar
        title, intro = subject, ""
        detail = "".join(f"<p style='font-size:14px'>{_esc(line)}</p>" for line in body.split("\n") if line.strip())
        meta = ""
    return (f"<div style='background:#f4f4f5;padding:24px 0'><div style='max-width:560px;margin:0 auto;background:#fff;"
            f"font-family:Helvetica,Arial,sans-serif;color:#111'>{head}<div style='padding:24px'>"
            f"<h2 style='margin:0 0 6px;font-size:22px'>{_esc(title)}</h2>"
            + (f"<p style='margin:0 0 4px;color:#555;font-size:14px'>{intro}</p>" if intro else "")
            + (f"<p style='margin:0 0 16px;color:#555;font-size:13px'>{meta}</p>" if meta else "")
            + f"{detail}</div><div style='padding:16px 24px;background:#fafafa;color:#888;font-size:11px;line-height:1.5'>"
            f"{store} · Questions? Reply to this email.<br>"
            f"Demo store for the Owed project: fictional retailer, real products.</div></div></div>")


def imap() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    conn.login(settings.gmail_address, settings.gmail_app_password.replace(" ", ""))
    return conn


def already_there(conn: imaplib.IMAP4_SSL, mid: str) -> bool:
    typ, data = conn.uid("search", None, "HEADER", "Message-ID", mid)
    return typ == "OK" and bool((data[0] or b"").split())


def label(conn: imaplib.IMAP4_SSL, mid: str) -> None:
    typ, data = conn.uid("search", None, "HEADER", "Message-ID", mid)
    for uid in (data[0] or b"").split() if typ == "OK" else []:
        conn.uid("store", uid, "+X-GM-LABELS", f"({LABEL})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--inbox", choices=["demo", "test", "all"], default="test")
    p.add_argument("--mode", choices=["append", "send"], default="append")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--remove", action="store_true", help="move every email this script added to the trash")
    a = p.parse_args()

    if not (settings.gmail_address and settings.gmail_app_password):
        sys.exit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env first (myaccount.google.com/apppasswords).")

    if a.remove:  # by Message-ID, so only emails this script added are touched
        conn = imap()
        conn.select("INBOX")
        uids = []
        for row in load("all"):
            typ, data = conn.uid("search", None, "HEADER", "Message-ID", message_id(row))
            uids += (data[0] or b"").split() if typ == "OK" else []
        print(f"Moving {len(uids)} test email(s) to the trash in {settings.gmail_address}" + (" (dry run)" if a.dry_run else ""))
        if not a.dry_run:
            for uid in uids:
                conn.uid("store", uid, "+X-GM-LABELS", "(\\Trash)")
        conn.logout()
        return 0

    rows = load(a.inbox)
    print(f"{len(rows)} email(s) from the '{a.inbox}' inbox -> {settings.gmail_address} (mode: {a.mode})"
          + (" - DRY RUN" if a.dry_run else ""))
    conn = imap()
    conn.select("INBOX")
    if a.mode == "append" and not a.dry_run:
        conn.create(LABEL)  # fails harmlessly if the label exists
    smtp = None
    added = skipped = 0
    try:
        for row in rows:
            mid = message_id(row)
            tag = f"  {row['_inbox']:<4} {row['id']:<9} {row['subject'][:60]}"
            if already_there(conn, mid):
                print(tag + "  (already there, skipped)")
                skipped += 1
                continue
            print(tag)
            if a.dry_run:
                continue
            msg = build(row, a.mode)
            if a.mode == "append":
                when = imaplib.Time2Internaldate(datetime.fromisoformat(row["date"].replace("Z", "+00:00")).timestamp())
                typ, resp = conn.append("INBOX", None, when, msg.as_bytes())
                if typ != "OK":
                    raise RuntimeError(f"IMAP append failed: {resp}")
            else:
                if smtp is None:
                    smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
                    smtp.login(settings.gmail_address, settings.gmail_app_password.replace(" ", ""))
                smtp.send_message(msg)
                time.sleep(1)  # stay well below Gmail's sending limits
            added += 1
        if not a.dry_run and added:
            time.sleep(3)  # let Gmail index the new messages before labelling them
            conn.select("INBOX")
            for row in rows:
                label(conn, message_id(row))
    finally:
        if smtp:
            smtp.quit()
        conn.logout()

    print(f"\nDone: {added} added, {skipped} already there.")
    q = settings.gmail_query
    if "label:owed" not in q:
        print("\nImportant: Owed only reads emails matching GMAIL_QUERY, currently:\n"
              f"  {q}\n"
              "Self-made test emails are not in Gmail's Purchases category, so set this in .env (and on Render):\n"
              f'  GMAIL_QUERY="{RECOMMENDED_QUERY}"\n'
              "That reads the emails labelled 'owed' plus real receipts by subject (e.g. one you forward live on stage).")
    expected = {"demo": "$263.50", "test": "$45.00 + 1 waiting for approval",
                "all": "$308.50 + 1 waiting for approval"}[a.inbox]
    print(f"\nThen click Run agent. Expected: {expected}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
