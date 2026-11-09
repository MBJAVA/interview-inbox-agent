"""Fixture-backed Gmail. Reads fixtures/emails/*.json; label state lives in the data dir.

Selection: MOCK_INBOX_FILES=s01_clear_invite,s12_role_in_application (default: every
non-demo fixture). Messages dated after the (mock) clock are invisible, like real mail
that hasn't arrived yet.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path

from core import clock
from core.config import ROOT, path

FIXTURES = ROOT / "fixtures" / "emails"


def _selected_files() -> list[Path]:
    sel = os.getenv("MOCK_INBOX_FILES")
    files = sorted(FIXTURES.glob("*.json"))
    if sel:
        names = {s.strip() for s in sel.split(",") if s.strip()}
        return [f for f in files if f.stem in names]
    return [f for f in files if not f.stem.startswith("demo_")]


def all_messages() -> list[dict]:
    msgs = []
    for f in _selected_files():
        msgs.extend(json.loads(f.read_text())["messages"])
    now = clock.now()
    visible = [m for m in msgs if datetime.fromisoformat(m["date"]) <= now]
    return sorted(visible, key=lambda m: m["date"])


def _labels_file() -> Path:
    return path("db").parent / "mock_gmail_labels.json"


def _load_labels() -> dict:
    f = _labels_file()
    return json.loads(f.read_text()) if f.exists() else {}


def get_labels(message_id: str) -> list[str]:
    return _load_labels().get(message_id, [])


def apply_label(message_id: str, label: str) -> dict:
    if not any(m["id"] == message_id for m in all_messages()):
        raise KeyError(f"message {message_id} not found")
    labels = _load_labels()
    cur = labels.setdefault(message_id, [])
    # Interview Prep/* labels are mutually exclusive status labels.
    if label.startswith("Interview Prep/"):
        cur[:] = [x for x in cur if not x.startswith("Interview Prep/")]
    if label not in cur:
        cur.append(label)
    _labels_file().write_text(json.dumps(labels, indent=1))
    return {"message_id": message_id, "labels": cur}


def _matches(m: dict, query: str) -> bool:
    q = query
    now = clock.now()
    for num, unit in re.findall(r"newer_than:(\d+)([dhm])", q):
        delta = {"d": timedelta(days=int(num)), "h": timedelta(hours=int(num)), "m": timedelta(days=30 * int(num))}[unit]
        if datetime.fromisoformat(m["date"]) < now - delta:
            return False
    for ts in re.findall(r"after:(\d{9,})", q):
        if datetime.fromisoformat(m["date"]).timestamp() <= int(ts):
            return False
    if "-category:promotions" in q and m.get("category") == "promotions":
        return False
    if "-category:social" in q and m.get("category") == "social":
        return False
    q = re.sub(r"(newer_than|after|-category|category|in|label|has):\S+", " ", q)
    sender = m["from"].lower()
    for val in re.findall(r"from:(\S+)", q):
        if val.lower().strip('"') not in sender:
            return False
    for val in re.findall(r"subject:\"([^\"]+)\"|subject:(\S+)", q):
        v = (val[0] or val[1]).lower()
        if v not in m["subject"].lower():
            return False
    q = re.sub(r"(from|subject):(\"[^\"]+\"|\S+)", " ", q)
    hay = (m["subject"] + " " + m["body"] + " " + m["from"]).lower()
    phrases = re.findall(r"\"([^\"]+)\"", q)
    for p in phrases:
        if p.lower() not in hay:
            return False
    q = re.sub(r"\"[^\"]+\"", " ", q)
    terms = [t.lower() for t in q.split() if t.upper() not in {"OR", "AND"}]
    if " OR " in query:
        return not terms or any(t in hay for t in terms)
    return all(t in hay for t in terms)


def search(query: str, max_results: int = 10) -> list[dict]:
    res = [m for m in all_messages() if _matches(m, query)]
    res = sorted(res, key=lambda m: m["date"], reverse=True)[:max_results]
    return [
        {"id": m["id"], "thread_id": m["thread_id"], "from": m["from"], "subject": m["subject"], "date": m["date"],
         "snippet": m["body"][:160]}
        for m in res
    ]


def get_message_raw(message_id: str) -> dict:
    for m in all_messages():
        if m["id"] == message_id:
            return m
    raise KeyError(f"message {message_id} not found")


def get_thread_raw(thread_id: str) -> list[dict]:
    msgs = [m for m in all_messages() if m["thread_id"] == thread_id]
    if not msgs:
        raise KeyError(f"thread {thread_id} not found")
    return msgs


def sender_domain(sender: str) -> str:
    addr = parseaddr(sender)[1]
    return addr.split("@")[-1].lower() if "@" in addr else ""


def registrable_domain(domain: str) -> str:
    """talent.paypal.com -> paypal.com. Recruiting subdomains of a company domain are the company itself."""
    parts = [p for p in (domain or "").lower().split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain
