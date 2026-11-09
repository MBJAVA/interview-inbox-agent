"""Parent graph: explicit control flow between plain-code steps and the four agent subgraphs.

mode=email:      load_email -> triage -> {extraction | close_record | persist}
                 extraction -> {link_identity | persist (waiting/past)} ; link_identity -> {curator | write_doc}
                 curator -> write_doc -> persist
mode=feedback:   load_record -> feedback -> {curator -> write_doc | reply_feedback} -> persist
mode=rebuild:    load_record -> curator -> write_doc -> persist
mode=correction: load_record -> extraction -> link_identity -> curator -> write_doc -> persist   (answer after a timeout)

Subgraphs are compiled without a checkpointer and inherit the parent's SqliteSaver, so an
interrupt() inside triage/extraction is persisted on the parent thread and resumed with
Command(resume=...).
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from core import clock, db
from core.config import budget, config, path
from graph import identity
from graph.common import RETRY
from graph.curator import build_curator_graph
from graph.extraction import build_extraction_graph
from graph.feedback import build_feedback_graph
from graph.output import build_sections, doc_title, fmt_dt
from graph.state import AgentState
from graph.triage import build_triage_graph
from tools import comments as comments_backend
from tools import gmail
from tools.drive_docs import render_markdown, write_doc as drive_write
from tools.mocks.gmail_mock import registrable_domain, sender_domain

AGENCY_HINTS = ("recruit", "staffing", "talent", "agency", "search", "hire")


def _run_reset(mode: str) -> dict:
    return {
        "next_step": None, "skip_curation": False, "refine_scope": None, "revision_note": None,
        "handoff_answer": None, "pending_handoff": None, "budget_exhausted": [], "tool_path": {"__replace__": []},
        "tool_call_count": 0, "token_count": 0, "iteration_count": 0, "step_count": 0, "last_error": None,
        "conflicts": [], "checked_links": {"__replace__": True}, "outcome": None, "classification": None,
        "tool_call_budget": budget("per_feedback_tool_calls") if mode == "feedback" else budget("per_email_tool_calls"),
    }


def _empty_record_state() -> dict:
    return {"interview": {}, "questions": [], "field_sources": {"__replace__": True}, "guessed_fields": [],
            "missing_fields": [], "open_questions": [], "build_log": {"__replace__": []}, "revision_history": [],
            "company_process": None, "research_confidence": None, "difficulty_labels": {"__replace__": True},
            "unfilled_slots": [], "existing_record": None}


# ------------------------------------------------------------------ entry
def route_entry(state: AgentState) -> str:
    return "load_email" if state.get("mode", "email") == "email" else "load_record"


def load_email(state: AgentState) -> dict:
    email = gmail.fetch_message(state["email_message_id"])
    try:
        email["thread_message_count"] = len(gmail.fetch_thread(email["thread_id"]))
    except Exception:
        email["thread_message_count"] = 1
    pid = identity.provisional_id(email["thread_id"])
    upd = {**_run_reset("email"), "email": email, "provisional_id": pid}
    rec = identity.lookup(email["thread_id"])
    if rec:
        upd.update(identity.state_from_record(rec))
        upd["build_log"] = {"__replace__": (rec.get("build_log") or []) + [
            {"tool": "thread_lookup", "outcome": f"Gmail thread maps to record {rec['id']} (status {rec.get('interview', {}).get('status')})", "fallback": None}]}
    else:
        upd.update(_empty_record_state())
        upd["interview_id"] = pid
        upd["gmail_thread_ids"] = [email["thread_id"]]
    upd["gmail_thread_ids"] = sorted(set(upd["gmail_thread_ids"]) | {email["thread_id"]})
    return upd


def load_record(state: AgentState) -> dict:
    rec = db.get_record(state["interview_id"])
    if not rec:
        raise ValueError(f"no interview record {state['interview_id']}")
    upd = {**_run_reset(state.get("mode", "feedback")), **identity.state_from_record(rec)}
    if state.get("mode") == "correction":
        upd["handoff_answer"] = state.get("handoff_answer")
        if state.get("email_message_id"):
            email = gmail.fetch_message(state["email_message_id"])
            email["thread_message_count"] = 1
            upd["email"] = email
        upd["classification"] = {"label": "interview_invite", "confidence": "high", "reason": "correction after handoff timeout"}
        upd["conflicts"] = []
        upd["guessed_fields"] = []
        upd["open_questions"] = [q for q in rec.get("open_questions", []) if not q.startswith("Unanswered handoff")]
        # The answer replaces the guesses: clear guessed values so extraction re-derives them.
        interview = dict(rec.get("interview") or {})
        for f in rec.get("guessed_fields") or []:
            interview.pop(f, None)
        upd["interview"] = interview
        upd["existing_record"] = {**rec, "interview": interview}
    return upd


def after_load_record(state: AgentState) -> str:
    return {"feedback": "feedback", "rebuild": "curator", "correction": "extraction"}[state["mode"]]


# ------------------------------------------------------------------ routing after agents
def after_triage(state: AgentState) -> str:
    return {"extract": "extraction", "close": "close_record"}.get(state.get("next_step"), "persist")


def after_extraction(state: AgentState) -> str:
    step = state.get("next_step")
    if step == "complete":
        return "link_identity"
    if step == "closed_past" and (state.get("interview") or {}).get("doc_id"):
        return "write_doc"
    return "persist"


def link_identity(state: AgentState) -> dict:
    return identity.link(state)


def after_link(state: AgentState) -> str:
    if state.get("skip_curation") and state.get("questions"):
        return "write_doc"
    return "curator"


def close_record(state: AgentState) -> dict:
    existing = state.get("existing_record")
    if not existing:
        dom = sender_domain(state["email"]["sender"])
        for r in db.list_records():
            if dom and dom in (r.get("sender_domains") or []) and not any(h in registrable_domain(dom) for h in AGENCY_HINTS):
                existing = r
                break
    if not existing:
        return {"outcome": "rejection_or_offer for an untracked interview: labeled only",
                "build_log": [{"tool": "close_record", "outcome": "no matching record to close", "fallback": "label only"}]}
    upd = identity.state_from_record(existing)
    interview = dict(existing.get("interview") or {})
    interview["status"] = "closed"
    upd["interview"] = interview
    upd["gmail_thread_ids"] = sorted(set(existing.get("gmail_thread_ids", [])) | {state["email"]["thread_id"]})
    upd["revision_note"] = f"Closed: {state['classification']['reason']}"
    upd["build_log"] = {"__replace__": (existing.get("build_log") or []) + [
        {"tool": "close_record", "outcome": f"record {existing['id']} closed ({state['classification']['label']}); nothing deleted", "fallback": None}]}
    return upd


def after_close(state: AgentState) -> str:
    return "write_doc" if (state.get("interview") or {}).get("doc_id") else "persist"


def after_feedback(state: AgentState) -> str:
    return "curator" if state.get("next_step") == "curate" else "reply_feedback"


# ------------------------------------------------------------------ outputs
def _default_note(state: AgentState) -> str:
    mode = state.get("mode")
    cls = (state.get("classification") or {}).get("label")
    i = state.get("interview") or {}
    if state.get("revision_note"):
        return state["revision_note"]
    if mode == "rebuild":
        return "Rebuilt question list"
    if mode == "correction":
        return f"Corrected details from your answer: {(state.get('handoff_answer') or '')[:80]}"
    if not i.get("doc_id"):
        return "Created prep doc"
    if cls == "reschedule":
        return f"Rescheduled to {fmt_dt(i.get('interview_datetime'))}" + ("" if state.get("skip_curation") else " (round changed; questions re-curated)")
    if cls == "next_round":
        return f"Next round added: {', '.join(i.get('round_types') or [])} on {fmt_dt(i.get('interview_datetime'))}"
    if (state.get("pending_handoff") or {}).get("status") == "timed_out":
        return "Proceeded with best guess after unanswered handoff"
    return f"Updated from new email ({cls})"


def write_doc(state: AgentState) -> dict:
    interview = dict(state["interview"])
    history = list(state.get("revision_history") or [])
    note = _default_note(state)
    history.append({"date": clock.now_local().strftime("%Y-%m-%d %H:%M"), "note": note})
    view = {**state, "revision_history": history, "interview": interview}
    sections = build_sections(view)
    title = doc_title(interview)
    res = drive_write(interview.get("doc_id"), title, sections)
    interview["doc_id"] = res["doc_id"]
    interview["doc_url"] = res["url"]
    md = render_markdown({"title": title, "agent_sections": [{"heading": h, "body": b} for h, b in sections], "my_notes": ""})
    out = path("output") / f"{state['interview_id']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    return {"interview": interview, "revision_history": history, "revision_note": note,
            "build_log": [{"tool": "create_or_update_prep_doc", "outcome": ("created" if res["created"] else "updated") + f" {res['doc_id']}", "fallback": None}]}


def after_write(state: AgentState) -> str:
    return "reply_feedback" if state.get("mode") == "feedback" else "persist"


def reply_feedback(state: AgentState) -> dict:
    fb = dict(state.get("feedback") or {})
    cls = fb.get("classification")
    if cls in ("revision_request", "count_change"):
        fb["result_line"] = state.get("revision_note") or "Updated the question list."
    if fb.get("source") == "comment" and fb.get("comment_id") and fb.get("result_line"):
        comments_backend.reply(fb["doc_id"], fb["comment_id"], fb["result_line"],
                               resolve=cls in ("revision_request", "count_change", "question"))
    return {"feedback": fb, "outcome": f"feedback {cls}: {fb.get('result_line') or 'no action (note)'}"}


def persist(state: AgentState) -> dict:
    labels = config()["labels"]
    mode = state.get("mode", "email")
    interview = state.get("interview") or {}
    is_interview = bool(interview) and (state.get("next_step") not in (None, "label_only") or mode != "email") \
        and (interview.get("company") or interview.get("role") or interview.get("status") == "waiting")
    if mode == "email" and state.get("next_step") == "label_only":
        is_interview = False
    rec_id = None
    if is_interview:
        rec = identity.record_from_state(state)
        doms = set((state.get("existing_record") or {}).get("sender_domains") or [])
        if state.get("email"):
            doms.add(sender_domain(state["email"]["sender"]))
        rec["sender_domains"] = sorted(d for d in doms if d)
        db.save_record(rec)
        rec_id = rec["id"]
    elif mode == "email":
        stale = db.get_record(state.get("interview_id") or "")
        if stale and (stale.get("interview") or {}).get("status") == "needs_input" and not (stale.get("interview") or {}).get("company"):
            db.delete_record_row(stale["id"])  # placeholder created while a triage handoff was pending; not an interview
    if mode == "email":
        label = labels["waiting"] if interview.get("status") == "waiting" and is_interview else labels["processed"]
        try:
            gmail.label(state["email"]["message_id"], label)
        except Exception as e:  # labeling is best effort; recorded, not fatal
            return {"outcome": f"label failed: {e}"}
    cls = (state.get("classification") or {}).get("label")
    outcome = state.get("outcome") or (
        f"{cls or mode}: {interview.get('status', 'no record')}" + (f" -> doc {interview.get('doc_id')}" if interview.get("doc_id") else ""))
    return {"outcome": outcome, "interview_id": rec_id or state.get("interview_id")}


# ------------------------------------------------------------------ build
def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("load_email", load_email, retry_policy=RETRY)
    g.add_node("load_record", load_record, retry_policy=RETRY)
    g.add_node("triage", build_triage_graph())
    g.add_node("extraction", build_extraction_graph())
    g.add_node("link_identity", link_identity)
    g.add_node("curator", build_curator_graph())
    g.add_node("close_record", close_record)
    g.add_node("feedback", build_feedback_graph())
    g.add_node("write_doc", write_doc, retry_policy=RETRY)
    g.add_node("reply_feedback", reply_feedback, retry_policy=RETRY)
    g.add_node("persist", persist, retry_policy=RETRY)

    g.add_conditional_edges(START, route_entry, ["load_email", "load_record"])
    g.add_edge("load_email", "triage")
    g.add_conditional_edges("load_record", after_load_record, ["feedback", "curator", "extraction"])
    g.add_conditional_edges("triage", after_triage, ["extraction", "close_record", "persist"])
    g.add_conditional_edges("extraction", after_extraction, ["link_identity", "write_doc", "persist"])
    g.add_conditional_edges("link_identity", after_link, ["curator", "write_doc"])
    g.add_edge("curator", "write_doc")
    g.add_conditional_edges("close_record", after_close, ["write_doc", "persist"])
    g.add_conditional_edges("feedback", after_feedback, ["curator", "reply_feedback"])
    g.add_conditional_edges("write_doc", after_write, ["reply_feedback", "persist"])
    g.add_edge("reply_feedback", "persist")
    g.add_edge("persist", END)
    return g.compile(checkpointer=checkpointer)


@lru_cache(maxsize=4)
def _saver(p: str) -> SqliteSaver:
    conn = sqlite3.connect(p, check_same_thread=False)
    return SqliteSaver(conn)


def get_app():
    """The parent graph with the persistent SqliteSaver checkpointer."""
    return build_graph(_saver(str(path("checkpoints"))))


# For LangGraph Studio / `langgraph dev` (it supplies its own persistence).
graph = build_graph()
