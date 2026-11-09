"""Human handoff via interrupt(). The poller never blocks: the interrupt is persisted by the
SqliteSaver checkpointer, recorded in pending_handoffs, the email is labeled Needs Input,
and the run moves on. `cli.py answer` resumes with Command(resume={"type": "answer", ...});
the poller resumes expired ones with {"type": "timeout"}."""
from __future__ import annotations

from datetime import timedelta

from langgraph.types import interrupt

from core import clock, db
from core.config import config

TRIGGERS = {
    "low_triage_confidence": "Triage confidence is low (can't tell whether this email is about an interview).",
    "missing_required": "Required interview details could not be found within budget.",
    "conflict": "Sources conflict about the interview details.",
    "unknown_company": "The hiring company can't be identified.",
}


def ask_user(state: dict, trigger: str, question: str, found: dict) -> dict:
    """Pause the graph and wait for the user. Returns {"type": "answer", "text": ...} or {"type": "timeout"}."""
    created = clock.now()
    timeout = created + timedelta(hours=float(config()["handoff"]["timeout_hours"]))
    payload = {
        "trigger": trigger,
        "trigger_text": TRIGGERS[trigger],
        "question": question,
        "found": found,
        "interview_id": state.get("interview_id"),
        "message_id": (state.get("email") or {}).get("message_id"),
        "subject": (state.get("email") or {}).get("subject"),
        "created_at": created.isoformat(),
        "timeout_at": timeout.isoformat(),
    }
    # Idempotent side effect before the interrupt (the node re-runs on resume): make the
    # record visible in `cli.py list` with status needs_input.
    rec = db.get_record(state["interview_id"]) or {"id": state["interview_id"], "gmail_thread_ids": state.get("gmail_thread_ids", [])}
    if (rec.get("interview") or {}).get("status") != "closed":
        interview = dict(rec.get("interview") or state.get("interview") or {})
        interview["status"] = "needs_input"
        rec["interview"] = interview
        rec.setdefault("gmail_thread_ids", state.get("gmail_thread_ids", []))
        db.save_record(rec)
    response = interrupt(payload)
    if not isinstance(response, dict):
        response = {"type": "answer", "text": str(response)}
    response["payload"] = payload
    return response
