"""Automation (plain code, not an agent): the pollers and the CLI actions.

The poller never blocks on a human: when a graph run hits interrupt(), the handoff is
recorded as pending, the email is labeled 'Interview Prep/Needs Input', and the loop moves
on to the next email.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from langgraph.types import Command

from core import clock, db
from core.config import config, path
from graph.parent_graph import get_app
from tools import comments as comments_backend
from tools import gmail


class LockBusy(Exception):
    pass


@contextmanager
def run_lock():
    lock = path("lock")
    stale = timedelta(minutes=config()["poller"]["lock_stale_minutes"]).total_seconds()
    if lock.exists() and time.time() - lock.stat().st_mtime > stale:
        lock.unlink(missing_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as e:
        raise LockBusy(f"another run holds {lock}") from e
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 150,
            "run_name": "interview_prep", "tags": ["interview-prep-agent"]}


def _after_invoke(result: dict, thread_id: str, message_id: str | None) -> dict:
    """Record an interrupt as a pending handoff (non-blocking), or return the outcome."""
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        payload = interrupts[0].value
        db.add_pending(thread_id, payload.get("interview_id") or thread_id, message_id or payload.get("message_id"),
                       payload["question"], payload, payload["created_at"], payload["timeout_at"])
        if message_id:
            try:
                gmail.label(message_id, config()["labels"]["needs_input"])
            except Exception:
                pass
        # The interrupted subgraph hasn't returned to the parent yet; read its live state.
        sub = {}
        try:
            snap = get_app().get_state(_cfg(thread_id), subgraphs=True)
            for task in snap.tasks:
                if task.state is not None and getattr(task.state, "values", None):
                    sub = task.state.values
        except Exception:
            pass
        return {"status": "needs_input", "question": payload["question"], "trigger": payload["trigger"],
                "interview_id": payload.get("interview_id"), "classification": sub.get("classification") or result.get("classification"),
                "tool_path": sub.get("tool_path") or result.get("tool_path", [])}
    return {"status": "done", "outcome": result.get("outcome"), "interview_id": result.get("interview_id"),
            "classification": result.get("classification"), "tool_path": result.get("tool_path", []),
            "next_step": result.get("next_step")}


def process_email(message_id: str, force: bool = False) -> dict:
    if db.is_processed(message_id) and not force:
        return {"status": "skipped", "reason": "already processed"}
    email = gmail.fetch_message(message_id)
    thread_id = f"gt_{email['thread_id']}"
    # A new email on a thread with an open handoff supersedes it (the new email brings new info).
    for h in db.list_pending("pending"):
        if h["graph_thread_id"] == thread_id:
            db.set_pending_status(thread_id, "superseded")
    started = time.time()
    result = get_app().invoke({"mode": "email", "email_message_id": message_id}, _cfg(thread_id))
    out = _after_invoke(result, thread_id, message_id)
    out["seconds"] = round(time.time() - started, 2)
    out["message_id"] = message_id
    db.mark_processed(message_id, email["thread_id"], out.get("interview_id"), out.get("outcome") or out["status"])
    return out


def poll_query() -> str:
    pcfg = config()["poller"]
    last = db.meta_get("last_run_epoch")
    if last:
        since = int(float(last)) - 3600  # 1h overlap; processed ids make this idempotent
        return f"after:{since} {pcfg['base_query']}"
    return f"newer_than:{pcfg['first_run_days']}d {pcfg['base_query']}"


def resume(graph_thread_id: str, value: dict) -> dict:
    h = next((x for x in db.list_pending("pending") if x["graph_thread_id"] == graph_thread_id), None)
    result = get_app().invoke(Command(resume=value), _cfg(graph_thread_id))
    db.set_pending_status(graph_thread_id, "timed_out" if value.get("type") == "timeout" else "answered")
    out = _after_invoke(result, graph_thread_id, h["message_id"] if h else None)
    if h and h.get("message_id") and out["status"] == "done":
        db.mark_processed(h["message_id"], graph_thread_id.removeprefix("gt_"), out.get("interview_id"), out.get("outcome") or "done")
    return out


def process_expired_handoffs() -> list[dict]:
    out = []
    now = clock.now()
    for h in db.list_pending("pending"):
        if datetime.fromisoformat(h["timeout_at"]) <= now:
            r = resume(h["graph_thread_id"], {"type": "timeout"})
            r["handoff"] = h["question"]
            out.append(r)
    return out


def run_feedback(interview_id: str, text: str, source: str = "cli", comment_id: str | None = None,
                 doc_id: str | None = None, quoted_text: str = "") -> dict:
    rec = db.get_record(interview_id)
    if not rec:
        return {"status": "error", "error": f"no interview {interview_id}"}
    fb = {"text": text, "source": source, "comment_id": comment_id, "doc_id": doc_id or rec["interview"].get("doc_id"),
          "quoted_text": quoted_text}
    result = get_app().invoke({"mode": "feedback", "interview_id": interview_id, "feedback": fb}, _cfg(f"fb_{interview_id}"))
    fbo = result.get("feedback") or {}
    return {"status": "done", "classification": fbo.get("classification"), "line": fbo.get("result_line"),
            "tool_path": result.get("tool_path", []), "interview_id": interview_id}


def process_comments() -> list[dict]:
    out = []
    for rec in db.list_records():
        doc_id = (rec.get("interview") or {}).get("doc_id")
        if not doc_id or (rec.get("interview") or {}).get("status") == "closed":
            continue
        try:
            items = comments_backend.list_comments(doc_id)
        except Exception as e:
            out.append({"doc_id": doc_id, "status": "error", "error": str(e)})
            continue
        for c in items:
            if db.comment_processed(c["id"]):
                continue
            r = run_feedback(rec["id"], c["content"], "comment", c["id"], doc_id, c.get("quoted_text", ""))
            db.mark_comment(c["id"], doc_id, r.get("classification") or "error")
            r["comment_id"] = c["id"]
            out.append(r)
    return out


def run_once(include_comments: bool = True, query: str | None = None, limit: int | None = None) -> dict:
    """query/limit override the normal poll (handy for a first real run on one email); an
    overridden run does not move the watermark, so the normal schedule still sees everything."""
    with run_lock():
        started = clock.now()
        msgs = gmail.search(query or poll_query(), max_results=100)
        new = [m for m in msgs if not db.is_processed(m["id"])]
        new.sort(key=lambda m: m["date"])
        cap = limit or config()["poller"]["max_emails_per_run"]
        results = []
        for m in new[:cap]:
            try:
                r = process_email(m["id"])
            except Exception as e:  # one bad email never stops the run
                r = {"status": "error", "error": f"{type(e).__name__}: {e}", "message_id": m["id"]}
            r["subject"] = m["subject"]
            results.append(r)
        expired = process_expired_handoffs()
        fb = process_comments() if include_comments else []
        # Advance the watermark only when nothing was deferred, so capped runs never skip mail.
        if len(new) <= cap and not query:
            db.meta_set("last_run_epoch", str(started.timestamp()))
        return {"emails": results, "deferred": max(0, len(new) - cap), "expired_handoffs": expired, "comments": fb}


def answer(interview_id: str, text: str) -> dict:
    h = db.find_handoff(interview_id)
    if not h:
        return {"status": "error", "error": f"no handoff for {interview_id}"}
    if h["status"] == "pending":
        return resume(h["graph_thread_id"], {"type": "answer", "text": text})
    # Timed out earlier: the agent proceeded with a guess. Correct it and update the same doc.
    rec_id = h["record_id"]
    rec = db.get_record(rec_id) or db.record_for_gmail_thread(h["graph_thread_id"].removeprefix("gt_"))
    if not rec:
        return {"status": "error", "error": "record not found"}
    result = get_app().invoke({"mode": "correction", "interview_id": rec["id"], "handoff_answer": text,
                               "email_message_id": h["message_id"]}, _cfg(f"cx_{rec['id']}"))
    db.set_pending_status(h["graph_thread_id"], "answered_after_timeout")
    return _after_invoke(result, f"cx_{rec['id']}", None)


def rebuild(interview_id: str) -> dict:
    result = get_app().invoke({"mode": "rebuild", "interview_id": interview_id}, _cfg(f"rb_{interview_id}"))
    return {"status": "done", "outcome": result.get("outcome"), "unfilled": result.get("unfilled_slots"),
            "tool_path": result.get("tool_path", [])}
