#!/usr/bin/env python
"""Turn a saved email (.eml) into a mock-inbox fixture, then run the agent on it.

    python scripts/import_eml.py "path/to/Follow up.eml" --name real_followup
    MOCK_INBOX_FILES=real_followup MOCK_LLM=false python cli.py run --once

Keeps the plain-text body (HTML is stripped when there is no text part) and any
text/calendar (.ics) parts. Other attachments (images, PDFs) are dropped.
The Message-ID / In-Reply-To headers become a thread id so replies group together.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.common import html_to_text  # noqa: E402


def convert(path: Path) -> dict:
    msg = BytesParser(policy=policy.default).parse(path.open("rb"))
    plain, html, attachments = [], [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.is_multipart():
            continue
        fname = part.get_filename() or ""
        if ctype == "text/calendar" or fname.lower().endswith(".ics"):
            attachments.append({"filename": fname or "invite.ics", "mime_type": "text/calendar",
                                "content": part.get_content() if isinstance(part.get_content(), str)
                                else part.get_payload(decode=True).decode("utf-8", "replace")})
        elif ctype == "text/plain" and part.get_content_disposition() != "attachment":
            plain.append(part.get_content())
        elif ctype == "text/html" and part.get_content_disposition() != "attachment":
            html.append(part.get_content())
    body = "\n".join(plain).strip() or html_to_text("\n".join(html))
    root_id = (msg.get("References", "").split() or [msg.get("In-Reply-To") or msg.get("Message-ID") or path.name])[0]
    thread = "eml_" + hashlib.sha1(root_id.encode()).hexdigest()[:8]
    mid = "eml_" + hashlib.sha1((msg.get("Message-ID") or path.name).encode()).hexdigest()[:8]
    try:
        date = parsedate_to_datetime(msg["Date"]).isoformat()
    except Exception:
        date = "2026-11-04T09:00:00-08:00"
    return {"id": mid, "thread_id": thread, "from": str(msg.get("From", "")), "to": str(msg.get("To", "")),
            "subject": str(msg.get("Subject", "")), "date": date, "category": "primary", "labels": ["INBOX"],
            "body": body, "attachments": attachments}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("eml", nargs="+")
    p.add_argument("--name", default="imported_eml")
    args = p.parse_args()
    msgs = [convert(Path(e)) for e in args.eml]
    out = ROOT / "fixtures" / "emails" / f"{args.name}.json"
    out.write_text(json.dumps({"messages": msgs}, indent=2))
    for m in msgs:
        print(f"{m['id']}  {m['date']}  {m['subject']!r}  body={len(m['body'])} chars  ics={len(m['attachments'])}")
    print(f"Wrote {out.relative_to(ROOT)}. Note: in mock mode the clock is pinned to config.yaml mock.now; "
          "set AGENT_NOW to a time after the email's date if it's newer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
