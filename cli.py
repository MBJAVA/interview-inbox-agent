#!/usr/bin/env python
"""Interview Prep Inbox Agent CLI (no chat).

  python cli.py run --once | --every 15m
  python cli.py pending
  python cli.py answer <interview_id> "<answer>"
  python cli.py refine <interview_id> "<feedback>"
  python cli.py list
  python cli.py rebuild <interview_id>
  python cli.py demo
"""
from __future__ import annotations

import argparse
import re
import sys
import time

from core import db


def _print_email_result(r: dict) -> None:
    subj = r.get("subject") or r.get("message_id")
    if r["status"] == "needs_input":
        print(f"  ? {subj}\n      handoff ({r['trigger']}): {r['question']}\n      answer with: python cli.py answer {r['interview_id']} \"...\"")
    elif r["status"] == "done":
        cls = (r.get("classification") or {})
        print(f"  ✓ {subj}\n      {cls.get('label')} ({cls.get('confidence')}) -> {r.get('outcome')}  [{r.get('seconds', '?')}s]")
        if r.get("tool_path"):
            print(f"      path: {' > '.join(t.split('#')[0] for t in r['tool_path'])}")
    elif r["status"] == "error":
        print(f"  ✗ {subj}: {r['error']}")


def cmd_run(args) -> int:
    from core.runner import LockBusy, run_once

    every = None
    if args.every:
        m = re.fullmatch(r"(\d+)([smh])", args.every)
        if not m:
            print("--every must look like 15m, 1h, 90s")
            return 2
        every = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
    while True:
        try:
            res = run_once(include_comments=not args.query, query=args.query, limit=args.limit)
        except LockBusy as e:
            print(f"Another run is in progress ({e}); exiting.")
            return 0
        print(f"Processed {len(res['emails'])} email(s)" + (f", {res['deferred']} deferred to next run" if res["deferred"] else ""))
        for r in res["emails"]:
            _print_email_result(r)
        for r in res["expired_handoffs"]:
            print(f"  ⏱ handoff timed out -> proceeded with best guess: {r.get('outcome')}")
        for r in res["comments"]:
            print(f"  💬 comment {r.get('comment_id')}: {r.get('classification')} -> {r.get('line')}")
        if not every:
            return 0
        time.sleep(every)


def cmd_pending(args) -> int:
    items = db.list_pending("pending")
    if not items:
        print("No handoffs waiting on you.")
    for h in items:
        print(f"- {h['record_id']}  ({h['payload'].get('trigger')})\n    email: {h['payload'].get('subject')}\n"
              f"    question: {h['question']}\n    times out: {h['timeout_at']} (then I proceed with a marked best guess)")
    return 0


def cmd_answer(args) -> int:
    from core.runner import answer

    r = answer(args.interview_id, args.answer)
    if r["status"] == "error":
        print(r["error"])
        return 1
    if r["status"] == "needs_input":
        print(f"Still need input: {r['question']}")
        return 0
    print(f"Resumed: {r.get('outcome')}")
    return 0


def cmd_refine(args) -> int:
    from core.runner import run_feedback

    r = run_feedback(args.interview_id, args.feedback, "cli")
    if r["status"] == "error":
        print(r["error"])
        return 1
    print(f"[{r['classification']}] {r.get('line') or 'Noted; no change.'}")
    return 0


def cmd_list(args) -> int:
    recs = db.list_records()
    if not recs:
        print("No interview records yet.")
    for r in recs:
        i = r.get("interview", {})
        print(f"- {r['id']}: {i.get('company')} – {i.get('role')} | {i.get('status')} | {i.get('interview_datetime') or 'date TBD'} | "
              f"{i.get('doc_url') or 'no doc'} | {len(r.get('questions', []))} questions")
    return 0


def cmd_rebuild(args) -> int:
    from core.runner import rebuild

    r = rebuild(args.interview_id)
    print(f"Rebuilt: {r.get('outcome')}; unfilled: {r.get('unfilled') or 'none'}")
    return 0


def cmd_doctor(args) -> int:
    from scripts.doctor import run_doctor

    return run_doctor()


def cmd_demo(args) -> int:
    from scripts.demo import run_demo

    return run_demo()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cli.py", description="Autonomous Interview Prep Inbox Agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process new emails, doc comments, and expired handoffs")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--every")
    r.add_argument("--query", help="Gmail query to use instead of the normal poll, e.g. 'subject:\"Follow up\" newer_than:7d'")
    r.add_argument("--limit", type=int, help="process at most N emails this run")
    sub.add_parser("pending", help="list handoffs waiting on you")
    a = sub.add_parser("answer", help="answer a handoff")
    a.add_argument("interview_id")
    a.add_argument("answer")
    f = sub.add_parser("refine", help="give feedback on a prep doc")
    f.add_argument("interview_id")
    f.add_argument("feedback")
    sub.add_parser("list", help="list interview records")
    b = sub.add_parser("rebuild", help="re-run curation for an interview")
    b.add_argument("interview_id")
    sub.add_parser("demo", help="replay fixture emails across days (mock mode)")
    sub.add_parser("doctor", help="check model, search, Google and tracing setup (read-only)")
    args = p.parse_args(argv)
    return {"run": cmd_run, "pending": cmd_pending, "answer": cmd_answer, "refine": cmd_refine, "list": cmd_list,
            "rebuild": cmd_rebuild, "demo": cmd_demo, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
