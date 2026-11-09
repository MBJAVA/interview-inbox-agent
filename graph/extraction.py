"""Extraction agent: infer first, ask last.

    extraction_init -> extraction_model --(tool calls, budget left)--> extraction_tools -> extraction_model
                                       \\--(done / budget hit)--> extraction_extract -> extraction_validate
    extraction_validate --(complete | wait | closed_past)--> END   (next_step for the parent)
                        \\--(handoff)--> extraction_handoff --(answer)--> extraction_model   (loop with bonus budget)
                                                          \\--(timeout)--> END  (best guess, marked)

The model decides where to look (thread, inbox history, job post, research), in what order,
and when to stop. extraction_validate enforces the hard rules (ics beats body, time-zone
normalization, round-type confidence rule, level default, past interview, handoff triggers).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from email.utils import parseaddr

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from core import clock
from core.config import budget, config
from core.llm import get_model, prompt, structured
from graph.common import RETRY, clean_convo, context_block, enforce_tool_budget, log_tool_failures, reset_messages
from graph.handoff import ask_user
from graph.schemas import ExtractionResult
from graph.state import ROUND_TYPES, AgentState
from tools.common import untrusted
from tools.attachments import read_attachment
from tools.company_research import research_company_process
from tools.gmail import get_message, get_thread, search_inbox
from tools.job_post import fetch_job_post
from tools.mocks.gmail_mock import sender_domain
from tools.records import get_interview_record

EXTRACTION_TOOLS = [get_thread, get_message, search_inbox, fetch_job_post, research_company_process, get_interview_record,
                    read_attachment]


def normalize_dt(value: str | None) -> str | None:
    """Parse ISO 8601 and convert to the user's time zone. Naive times are assumed to be in the user's zone."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=clock.user_tz())
    return dt.astimezone(clock.user_tz()).isoformat()


def extraction_init(state: AgentState) -> dict:
    e = state["email"]
    existing = state.get("existing_record")
    ctx = {
        "today": clock.now_local().date().isoformat(),
        "user_timezone": config()["user"]["timezone"],
        "classification": state.get("classification"),
        "email": {"message_id": e["message_id"], "thread_id": e["thread_id"], "from": e["sender"],
                  "sender_domain": sender_domain(e["sender"]), "subject": e["subject"], "date": e["date"],
                  "body": untrusted(e["body"]), "calendar_invite": e.get("calendar_invite"),
                  "attachments": e.get("attachments") or []},
        "thread_message_count": e.get("thread_message_count", 1),
        "existing_interview_record": (existing or {}).get("interview"),
        "required_fields": config()["extraction"]["required_fields"],
    }
    msgs = reset_messages() + [SystemMessage(prompt("extraction")), HumanMessage(context_block(ctx))]
    if state.get("handoff_answer"):
        msgs.append(HumanMessage(f"The user answered an earlier question about this interview: {untrusted(state['handoff_answer'], 500)}"))
    return {"messages": msgs, "step_count": 0, "max_iterations": budget("extraction_max_iterations")}


def extraction_model(state: AgentState) -> dict:
    model = get_model("extraction").bind_tools(EXTRACTION_TOOLS)
    failures = log_tool_failures(state["messages"], "extraction")
    ai = model.invoke(state["messages"])
    ai, upd = enforce_tool_budget(state, ai, "extraction")
    upd["build_log"] = failures + upd.get("build_log", [])
    if ai.tool_calls and state.get("step_count", 0) + 1 >= state.get("max_iterations", 5):
        upd["budget_exhausted"] = list(upd.get("budget_exhausted") or state.get("budget_exhausted") or []) + [
            f"extraction: max_iterations ({state.get('max_iterations')}) reached"]
    return {"messages": [ai], **upd}


def after_model(state: AgentState) -> str:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None) and state.get("step_count", 0) < state.get("max_iterations", 5):
        return "extraction_tools"
    return "extraction_extract"


def extraction_extract(state: AgentState) -> dict:
    res: ExtractionResult = structured("extraction_result", ExtractionResult).invoke(
        [SystemMessage(prompt("extraction_result"))] + clean_convo(state["messages"])
        + [HumanMessage("Record the structured extraction result now.")])
    return {"messages": [HumanMessage("[extraction_result] " + res.model_dump_json())]}


def _tool_outputs(messages: list, name: str) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == name:
            try:
                out.append(json.loads(m.content))
            except Exception:
                pass
    return out


def _latest_result(messages: list) -> ExtractionResult:
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and isinstance(m.content, str) and m.content.startswith("[extraction_result] "):
            return ExtractionResult.model_validate_json(m.content[len("[extraction_result] "):])
    return ExtractionResult()


LEVELS = ("junior", "mid", "senior", "staff", "principal")
LEVEL_ALIASES = {
    "junior": ["i", "1", "entry", "entry-level", "entry level", "new grad", "associate", "l3", "e3", "sde i", "sde 1"],
    "mid": ["ii", "2", "intermediate", "mid-level", "mid level", "l4", "e4", "sde ii", "sde 2"],
    "senior": ["iii", "3", "sr", "sr.", "l5", "e5", "sde iii", "sde 3"],
    "staff": ["l6", "e6"],
    "principal": ["l7", "e7"],
}


def normalize_level(value: str | None) -> str | None:
    """Map whatever the model extracted ('II', 'Sr.', 'L5') to one of the configured levels; None if unknown."""
    v = (value or "").strip().lower()
    if v in LEVELS:
        return v
    for level, aliases in LEVEL_ALIASES.items():
        if v in aliases:
            return level
    return next((lv for lv in LEVELS if lv in v), None)


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def verify_body_source(value: str | None, src: str, email: dict) -> str:
    """If the model credits the email body (or just 'email') for a value that isn't in the body,
    relabel it with where it actually appears: subject, sender name, or sender domain."""
    if not value or not ("body" in src.lower() or src.strip().lower() == "email"):
        return src
    v = _norm(value)
    if not v or v in _norm(email.get("body")):
        return src
    if v in _norm(email.get("subject")):
        return "email subject"
    name = parseaddr(email.get("sender") or "")[0]
    if v in _norm(name):
        return "sender name"
    domain = sender_domain(email.get("sender") or "")
    if v in _norm(domain):
        return f"sender domain ({domain})"
    return "inferred (not stated in the email body)"


def extraction_validate(state: AgentState) -> dict:
    res = _latest_result(state["messages"])
    e = state["email"]
    cls = (state.get("classification") or {}).get("label")
    existing = (state.get("existing_record") or {}).get("interview") or {}
    base = dict(state.get("interview") or existing or {})
    sources: dict = {}
    guessed = [g for g in (state.get("guessed_fields") or []) if g not in ("company", "role", "round_types", "interview_datetime")]
    open_q = [q for q in (state.get("open_questions") or []) if not q.startswith(("Level unknown", "Round types inferred"))]
    log: list[dict] = []

    def take(field: str, fv):
        if fv and fv.value:
            base[field] = fv.value
            sources[field] = fv.source or "email"

    for f in ("company", "role", "level", "format", "job_post", "recruiter_name", "industry"):
        take(f, getattr(res, f))
    for f in ("company", "recruiter_name"):  # names should appear verbatim where the model says it found them
        if f in sources:
            sources[f] = verify_body_source(base.get(f), sources[f], e)
    if res.job_post_summary:
        base["job_post_summary"] = res.job_post_summary
    if res.focus_topics:
        base["focus_topics"] = res.focus_topics
        sources["focus_topics"] = res.focus_topics_source or "email"
    if res.notes:
        base["notes"] = res.notes
    if base.get("level") or base.get("role"):
        role = base.get("role") or ""
        lv = normalize_level(base.get("level")) or normalize_level(role) or normalize_level((role.split() or [""])[-1])
        if lv and lv != base.get("level"):
            sources["level"] = sources.get("level") or "inferred from role title"
        if lv:
            base["level"] = lv
        else:
            base.pop("level", None)  # unrecognized value: fall through to the default below, marked guessed

    # --- date/time: calendar invite beats the body; always normalize to the user's zone
    cal = e.get("calendar_invite") or {}
    if cal.get("start"):
        base["interview_datetime"] = normalize_dt(cal["start"])
        sources["interview_datetime"] = f"calendar invite (.ics, {cal.get('timezone')}), converted to {config()['user']['timezone']}"
        if res.interview_datetime.value and normalize_dt(res.interview_datetime.value) != base["interview_datetime"]:
            log.append({"tool": "extraction.validate", "outcome": "email body time differed from the .ics", "fallback": "used the calendar invite"})
    elif res.interview_datetime.value:
        base["interview_datetime"] = normalize_dt(res.interview_datetime.value)
        src = res.interview_datetime.source or "email"
        sources["interview_datetime"] = f"{src}, converted to {config()['user']['timezone']}"

    # --- round types + the research confidence rule
    process = (_tool_outputs(state["messages"], "research_company_process") or [None])[-1]
    company_process = process if process and process.get("ok") else state.get("company_process")
    rounds = [r for r in res.round_types if r in ROUND_TYPES]
    trigger, question = None, None
    date_only = cls == "reschedule" and (state.get("classification") or {}).get("reschedule_scope") == "date_only"
    if date_only and existing.get("round_types"):
        base["round_types"] = existing["round_types"]  # only the date moved; rounds stay as recorded
    elif rounds and res.round_types_stated:
        if cls == "next_round" and existing.get("round_types"):
            rounds = list(dict.fromkeys(existing["round_types"] + rounds))  # a later round adds to the record
        base["round_types"] = rounds
        sources["round_types"] = res.round_types_source or "email"
    elif existing.get("round_types") and not rounds:
        base["round_types"] = existing["round_types"]
    else:
        conf = (company_process or {}).get("confidence", "low")
        research_rounds = [r for r in (company_process or {}).get("rounds", []) if r in ROUND_TYPES]
        rounds = rounds or research_rounds
        if rounds and conf == "high":
            base["round_types"] = rounds
            sources["round_types"] = "company research (high confidence)"
        elif rounds and conf == "medium":
            base["round_types"] = rounds
            sources["round_types"] = "company research (medium confidence) - inferred, unconfirmed"
            guessed.append("round_types")
            open_q.append("Round types inferred from research (medium confidence), unconfirmed.")
        else:
            base.pop("round_types", None)
            trigger, question = "missing_required", (
                f"Which interview round types should I prepare for at {base.get('company') or 'this company'}"
                " (coding, AI-assisted coding, system design, low-level design, behavioral)? The emails don't say and research was inconclusive.")

    # --- level default
    if not base.get("level"):
        base["level"] = config()["extraction"]["default_level"]
        sources["level"] = "default (not found) - using the senior slot mix"
        guessed.append("level")
        open_q.append("Level unknown; used the senior slot mix.")

    # --- job post failures become notes, not silent gaps
    for jp in _tool_outputs(state["messages"], "fetch_job_post"):
        if not jp.get("ok"):
            note = f"Job post link failed ({jp.get('error')}); level/stack inferred without it."
            if note not in open_q:
                open_q.append(note)

    base.setdefault("status", "active")
    base.setdefault("count_overrides", existing.get("count_overrides", {}))
    base.setdefault("doc_id", existing.get("doc_id"))
    conflicts = list(res.conflicts)
    missing = [f for f in config()["extraction"]["required_fields"] if not base.get(f)]

    # --- decide (model's decision, then hard rules)
    decision = res.decision
    if decision == "handoff" and res.handoff_question:
        trigger = trigger or ("conflict" if conflicts else "unknown_company" if not base.get("company") else "missing_required")
        question = res.handoff_question
    if conflicts and not state.get("handoff_answer"):
        trigger = "conflict"
        question = res.handoff_question or f"Sources disagree: {conflicts[0]} Which is correct?"
    elif not base.get("company"):
        trigger, question = "unknown_company", res.handoff_question or (
            f"The email from {sender_domain(e['sender'])} doesn't name the hiring company. Which company is this interview with?")
    elif missing and not trigger and decision != "wait":
        trigger = "missing_required"
        question = res.handoff_question or f"I couldn't find: {', '.join(missing)}. Can you tell me?"
    if decision == "wait" and (base.get("interview_datetime") or cls not in ("scheduling_request", "interview_invite")):
        decision = "complete"  # waiting is only for a scheduling request whose time isn't set yet
    if decision == "wait" and base.get("company") and not conflicts:
        trigger = None

    upd = {"interview": base, "field_sources": sources, "guessed_fields": sorted(set(guessed)),
           "missing_fields": missing, "conflicts": conflicts, "open_questions": open_q,
           "company_process": company_process, "build_log": log}
    if company_process:
        upd["research_confidence"] = {"level": company_process.get("confidence"),
                                      "reason": company_process.get("confidence_reason") or company_process.get("notes", "")[:160],
                                      "sources": company_process.get("sources", [])}
    if trigger:
        upd["pending_handoff"] = {"question": question, "trigger": trigger}
        upd["next_step"] = "handoff"
        return upd
    if decision == "wait":
        base["status"] = "waiting"
        waiting_for = [f for f in ("interview_datetime", "role") if not base.get(f)]
        upd["open_questions"] = open_q + [f"Waiting for a later email with: {', '.join(waiting_for).replace('interview_datetime', 'the interview date/time')}."]
        # Prep early when we already know who and what kind of round: the later email updates the same doc.
        early = bool(base.get("company") and base.get("round_types"))
        upd["next_step"] = "complete" if early else "wait"
        # A decision, not a failure: no fallback, so the doc lists it under steps, not failures.
        upd["build_log"] = log + [{"tool": "extraction", "outcome": f"waiting: {res.decision_reason or 'time not set yet'}"
                                   + ("; building the prep doc early (company and round known)" if early else ""),
                                   "fallback": None}]
        return upd
    dt = base.get("interview_datetime")
    if dt and datetime.fromisoformat(dt) < clock.now():
        base["status"] = "closed"
        upd["next_step"] = "closed_past"
        upd["build_log"] = log + [{"tool": "extraction", "outcome": f"interview time {dt} is in the past: record closed, curation skipped", "fallback": None}]
        return upd
    base["status"] = "active"
    upd["next_step"] = "complete"
    return upd


def extraction_handoff(state: AgentState) -> dict:
    ph = state["pending_handoff"]
    found = {k: v for k, v in (state.get("interview") or {}).items() if v and k not in ("count_overrides",)}
    resp = ask_user(state, ph["trigger"], ph["question"],
                    {"fields": found, "sources": state.get("field_sources"), "conflicts": state.get("conflicts")})
    if resp["type"] == "timeout":
        interview = dict(state.get("interview") or {})
        guessed = list(state.get("guessed_fields") or [])
        sources = {}
        if not interview.get("company"):
            interview["company"] = f"Unknown company (via {sender_domain(state['email']['sender'])})"
            guessed.append("company"); sources["company"] = "best guess after handoff timeout"
        if not interview.get("role"):
            interview["role"] = "Software Engineer"
            guessed.append("role"); sources["role"] = "best guess after handoff timeout"
        if not interview.get("round_types"):
            interview["round_types"] = ["coding", "behavioral"]
            guessed.append("round_types"); sources["round_types"] = "best guess after handoff timeout (most common loop)"
        if ph["trigger"] == "conflict":
            guessed.append("round_types")
            sources["round_types"] = "best guess after handoff timeout: used the most recent email"
        interview["status"] = "active"
        dt = interview.get("interview_datetime")
        nxt = "closed_past" if dt and datetime.fromisoformat(dt) < clock.now() else "complete"
        return {"interview": interview, "guessed_fields": sorted(set(guessed)), "field_sources": sources,
                "open_questions": (state.get("open_questions") or []) + [f"Unanswered handoff (timed out): {ph['question']}"],
                "pending_handoff": {**ph, "status": "timed_out"}, "next_step": nxt,
                "build_log": [{"tool": "handoff", "outcome": f"no answer within {config()['handoff']['timeout_hours']}h",
                               "fallback": "proceeded with best guess; guessed fields marked"}]}
    answer = resp.get("text", "")
    return {"handoff_answer": answer, "pending_handoff": None, "conflicts": [],
            "max_iterations": (state.get("max_iterations") or 5) + budget("extraction_bonus_iterations_after_answer"),
            "messages": [HumanMessage(f"The user answered your question ('{ph['question']}'): {untrusted(answer, 500)}\n"
                                      "Treat the answer as authoritative for the interview details. Continue.")],
            "build_log": [{"tool": "handoff", "outcome": f"user answered: {answer[:80]}", "fallback": None}]}


def after_validate(state: AgentState) -> str:
    return "extraction_handoff" if state.get("next_step") == "handoff" else END


def after_handoff(state: AgentState) -> str:
    return "extraction_model" if state.get("handoff_answer") and not state.get("pending_handoff") else END


def build_extraction_graph():
    g = StateGraph(AgentState)
    g.add_node("extraction_init", extraction_init)
    g.add_node("extraction_model", extraction_model, retry_policy=RETRY)
    g.add_node("extraction_tools", ToolNode(EXTRACTION_TOOLS), retry_policy=RETRY)
    g.add_node("extraction_extract", extraction_extract, retry_policy=RETRY)
    g.add_node("extraction_validate", extraction_validate)
    g.add_node("extraction_handoff", extraction_handoff)
    g.add_edge(START, "extraction_init")
    g.add_edge("extraction_init", "extraction_model")
    g.add_conditional_edges("extraction_model", after_model, ["extraction_tools", "extraction_extract"])
    g.add_edge("extraction_tools", "extraction_model")
    g.add_edge("extraction_extract", "extraction_validate")
    g.add_conditional_edges("extraction_validate", after_validate, ["extraction_handoff", END])
    g.add_conditional_edges("extraction_handoff", after_handoff, ["extraction_model", END])
    return g.compile()
