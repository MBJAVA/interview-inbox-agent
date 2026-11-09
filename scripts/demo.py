"""`cli.py demo`: reset mock state and replay the Northwind Labs fixture emails across days
(invite -> vague next-round email on a NEW thread -> reschedule -> refine request -> rejection),
printing each step's decision path, to show state carried across days in one record and one doc."""
from __future__ import annotations

import os
import shutil
from datetime import datetime

from core.config import ROOT, mock_llm

DEMO_DATA = ROOT / "data" / "demo"
DEMO_OUT = ROOT / "output" / "demo"


def _setup() -> None:
    os.environ.update({"MOCK_GOOGLE": "true", "MOCK_WEB": "true", "MOCK_INBOX_FILES": "demo_northwind",
                       "AGENT_DATA_DIR": str(DEMO_DATA), "AGENT_OUTPUT_DIR": str(DEMO_OUT)})
    shutil.rmtree(DEMO_DATA, ignore_errors=True)
    shutil.rmtree(DEMO_OUT, ignore_errors=True)
    from core import db

    db.reset_connection()


def _path(r: dict) -> str:
    return " > ".join(t.split("#")[0] for t in r.get("tool_path", [])) or "(no tools)"


def _record_summary() -> str:
    from core import db

    recs = db.list_records()
    lines = []
    for r in recs:
        i = r["interview"]
        counts: dict = {}
        for q in r.get("questions", []):
            counts.setdefault(q["round_type"], 0)
            counts[q["round_type"]] += 1
        lines.append(f"    record {r['id']}: {i.get('company')} | {i.get('role')} | status={i.get('status')} | "
                     f"{i.get('interview_datetime')} | rounds={i.get('round_types')} | questions={counts} | doc={i.get('doc_id')}")
    return "\n".join(lines)


def run_demo() -> int:
    _setup()
    from core import clock
    from core.runner import run_feedback, run_once
    from tools.mocks import drive_mock

    print(f"Demo (mock Google + web; model: {'offline policy model' if mock_llm() else os.getenv('MODEL_NAME', 'gpt-6.1-terra')})\n")
    steps = [
        ("Day 1  Mon Nov 2", "2026-11-02T10:30:00-08:00", "run", None),
        ("Day 5  Fri Nov 6", "2026-11-06T09:30:00-08:00", "run", None),
        ("Day 6  Sat Nov 7", "2026-11-07T09:30:00-08:00", "run", None),
        ("Day 6  Sat Nov 7 (you)", "2026-11-07T12:00:00-08:00", "refine", "The system design questions are not relevant to payments"),
        ("Day 15 Mon Nov 16", "2026-11-16T09:30:00-08:00", "run", None),
    ]
    for label, now, action, arg in steps:
        clock.set_now(datetime.fromisoformat(now))
        print(f"== {label} ==")
        if action == "run":
            res = run_once(include_comments=False)
            for r in res["emails"]:
                cls = r.get("classification") or {}
                print(f"  email: {r.get('subject')}")
                print(f"    triage: {cls.get('label')} ({cls.get('confidence')}) - {cls.get('reason')}")
                print(f"    path:   {_path(r)}")
                print(f"    result: {r.get('outcome') or r.get('question')}")
        else:
            from core import db

            rid = db.list_records()[0]["id"]
            print(f"  python cli.py refine {rid} \"{arg}\"")
            r = run_feedback(rid, arg, "cli")
            print(f"    feedback: {r['classification']} | path: {_path(r)}")
            print(f"    -> {r.get('line')}")
        print(_record_summary())
        print()
    clock.set_now(None)
    docs = drive_mock.list_docs()
    print(f"Docs created: {len(docs)} (expected 1). Doc updates: {docs[0]['update_count'] if docs else 0}.")
    if docs:
        print(f"Doc file: {DEMO_OUT / 'mock_drive' / (docs[0]['doc_id'] + '.md')}")
        print("Revision history:")
        from core import db

        for h in db.list_records()[0].get("revision_history", []):
            print(f"  - {h['date']}: {h['note']}")
    return 0 if len(docs) == 1 else 1
