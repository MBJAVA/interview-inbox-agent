#!/usr/bin/env python
"""Put a fixture's test emails into your real Gmail inbox (for end-to-end testing).

    python scripts/seed_gmail.py demo_inbox_test_set --to you@gmail.com            # dry run: lists what it would do
    python scripts/seed_gmail.py demo_inbox_test_set --to you@gmail.com --yes      # insert
    python scripts/seed_gmail.py --cleanup --to you@gmail.com --yes                # move inserted ones to Trash

Uses Gmail's messages.insert: the messages are placed straight into the mailbox (nothing is sent, no
mail leaves your account), with the fixture's From/Date headers, so sender-domain inference and dates
behave like real mail. Needs the existing gmail.modify scope; this is a test utility, not an agent tool.
Messages sharing a fixture thread_id are threaded together. Inserted ids are saved for --cleanup.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "data" / "seeded_gmail_ids.json"
CATEGORY_LABELS = {"promotions": "CATEGORY_PROMOTIONS", "social": "CATEGORY_SOCIAL", "updates": "CATEGORY_UPDATES",
                   "forums": "CATEGORY_FORUMS", "primary": "CATEGORY_PERSONAL"}


def build_mime(m: dict, to: str, msgid: str, parent: str | None) -> bytes:
    e = EmailMessage()
    e["From"], e["To"], e["Subject"] = m["from"], to, m["subject"]
    e["Date"] = format_datetime(datetime.fromisoformat(m["date"]))
    e["Message-ID"] = msgid
    if parent:
        e["In-Reply-To"] = e["References"] = parent
    e.set_content(m["body"])
    for a in m.get("attachments") or []:
        maintype, subtype = a["mime_type"].split("/", 1)
        if "content" in a:
            data = a["content"].encode()
        else:
            data = (ROOT / a["path"]).read_bytes()
        e.add_attachment(data, maintype=maintype, subtype=subtype, filename=a["filename"])
    return e.as_bytes()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("fixture", nargs="?", default="demo_inbox_test_set")
    p.add_argument("--to", required=True, help="Your Gmail address; must match the signed-in account.")
    p.add_argument("--yes", action="store_true", help="Actually write to Gmail (default is a dry run).")
    p.add_argument("--cleanup", action="store_true", help="Move previously inserted messages to Trash.")
    args = p.parse_args()

    if args.cleanup:
        ids = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
        print(f"{len(ids)} seeded message(s) to trash.")
        if not args.yes or not ids:
            return 0
    else:
        msgs = json.loads((ROOT / "fixtures" / "emails" / f"{args.fixture}.json").read_text())["messages"]
        for m in msgs:
            print(f"{m['date']}  [{m.get('category', 'primary')}]  {m['from']}\n    {m['subject']}")
        if not args.yes:
            print(f"\nDry run: {len(msgs)} message(s) would be inserted into {args.to}. Re-run with --yes.")
            return 0

    from tools.google_auth import service

    svc = service("gmail", "v1").users()
    me = svc.getProfile(userId="me").execute()["emailAddress"]
    if me.lower() != args.to.lower():
        print(f"Signed in as {me}, not {args.to}. Delete token.json and sign in with the right account.")
        return 1

    if args.cleanup:
        for i in ids:
            svc.messages().trash(userId="me", id=i).execute()
        LEDGER.write_text("[]")
        print(f"Trashed {len(ids)} message(s).")
        return 0

    seeded = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
    threads: dict[str, tuple[str, str]] = {}  # fixture thread_id -> (gmail threadId, last Message-ID)
    for m in sorted(msgs, key=lambda x: x["date"]):
        msgid = make_msgid(domain="seed.test")
        gthread, parent = threads.get(m["thread_id"], (None, None))
        labels = ["INBOX", "UNREAD", CATEGORY_LABELS.get(m.get("category", "primary"), "CATEGORY_PERSONAL")]
        body = {"raw": base64.urlsafe_b64encode(build_mime(m, args.to, msgid, parent)).decode(), "labelIds": labels}
        if gthread:
            body["threadId"] = gthread
        res = svc.messages().insert(userId="me", body=body, internalDateSource="dateHeader").execute()
        threads[m["thread_id"]] = (res["threadId"], msgid)
        seeded.append(res["id"])
        print(f"inserted {res['id']}  {m['subject']}")
    LEDGER.write_text(json.dumps(seeded, indent=1))
    print(f"\nDone: {len(msgs)} message(s) in {args.to}. Ids saved to {LEDGER.relative_to(ROOT)} for --cleanup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
