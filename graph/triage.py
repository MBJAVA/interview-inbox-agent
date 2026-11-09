"""Triage agent: a hand-written StateGraph loop.

    triage_init -> triage_model --(tool calls & budget left)--> triage_tools -> triage_model
                               \\--(no tool calls / budget hit)--> triage_classify -> triage_route
    triage_route --(low confidence)--> triage_handoff (interrupt) -> END
                 \\--> END   (sets next_step for the parent graph)

The model decides whether it needs the thread / record before classifying.
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from core.config import budget, config
from core.llm import get_model, prompt, structured
from graph.common import RETRY, clean_convo, context_block, enforce_tool_budget, log_tool_failures, reset_messages
from graph.handoff import ask_user
from graph.schemas import TriageDecision
from graph.state import AgentState
from tools.common import untrusted
from tools.gmail import get_thread, search_inbox
from tools.records import get_interview_record, summarize_record

TRIAGE_TOOLS = [get_thread, search_inbox, get_interview_record]

EXTRACT_LABELS = {"interview_invite", "scheduling_request", "interview_confirmation", "next_round"}


def triage_init(state: AgentState) -> dict:
    e = state["email"]
    existing = state.get("existing_record")
    ctx = {
        "email": {"message_id": e["message_id"], "thread_id": e["thread_id"], "from": e["sender"],
                  "subject": e["subject"], "date": e["date"], "body": untrusted(e["body"]),
                  "has_calendar_invite": bool(e.get("calendar_invite"))},
        "thread_message_count": e.get("thread_message_count", 1),
        "existing_interview_record": summarize_record(existing) if existing else None,
    }
    return {
        "messages": reset_messages() + [SystemMessage(prompt("triage")), HumanMessage(context_block(ctx))],
        "step_count": 0,
        "max_iterations": budget("triage_max_iterations"),
    }


def triage_model(state: AgentState) -> dict:
    model = get_model("triage").bind_tools(TRIAGE_TOOLS)
    failures = log_tool_failures(state["messages"], "triage")
    ai = model.invoke(state["messages"])
    ai, upd = enforce_tool_budget(state, ai, "triage")
    upd["build_log"] = failures + upd.get("build_log", [])
    return {"messages": [ai], **upd}


def after_model(state: AgentState) -> str:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None) and state.get("step_count", 0) < state.get("max_iterations", 3):
        return "triage_tools"
    return "triage_classify"


def triage_classify(state: AgentState) -> dict:
    convo = clean_convo(state["messages"])
    decision: TriageDecision = structured("triage_classify", TriageDecision).invoke(
        [SystemMessage(prompt("triage"))] + convo + [HumanMessage("Record your classification now.")])
    cls = decision.model_dump()
    return {"classification": cls,
            "build_log": [{"tool": "triage", "outcome": f"classified {cls['label']} ({cls['confidence']}): {cls['reason']}",
                           "fallback": None}]}


def triage_route(state: AgentState) -> dict:
    cls = state["classification"]
    label = cls["label"]
    existing = state.get("existing_record")
    if cls["confidence"] in config()["triage"]["low_confidence"]:
        return {"next_step": "handoff"}
    if label == "reschedule":
        if not existing:
            return {"next_step": "extract", "skip_curation": False,
                    "build_log": [{"tool": "triage.route", "outcome": "reschedule for an untracked interview: treated as a new invite", "fallback": None}]}
        return {"next_step": "extract", "skip_curation": cls.get("reschedule_scope") != "round_changed"}
    if label in EXTRACT_LABELS:
        return {"next_step": "extract", "skip_curation": False}
    if label == "rejection_or_offer":
        return {"next_step": "close"}
    return {"next_step": "label_only"}


def triage_handoff(state: AgentState) -> dict:
    cls = state["classification"]
    resp = ask_user(state, "low_triage_confidence",
                    f"Is the email '{state['email']['subject']}' about one of your interviews? "
                    f"(My best guess: {cls['label']} - {cls['reason']}) Reply 'yes' or 'no'.",
                    {"best_guess": cls})
    if resp["type"] == "timeout":
        return {"next_step": "extract" if cls["label"] in EXTRACT_LABELS else "label_only",
                "open_questions": (state.get("open_questions") or []) + [
                    f"Unanswered: was '{state['email']['subject']}' about an interview? Proceeded with best guess: {cls['label']}."],
                "build_log": [{"tool": "handoff", "outcome": "timed out", "fallback": f"used best guess {cls['label']}"}]}
    yes = resp.get("text", "").strip().lower().startswith(("y", "it is", "yes"))
    new = {**cls, "label": "interview_invite" if yes else "not_relevant", "confidence": "high",
           "reason": f"user answered: {resp.get('text', '')[:80]}"}
    return {"classification": new, "next_step": "extract" if yes else "label_only", "skip_curation": False,
            "build_log": [{"tool": "handoff", "outcome": f"user answered '{resp.get('text', '')[:60]}'", "fallback": None}]}


def after_route(state: AgentState) -> str:
    return "triage_handoff" if state.get("next_step") == "handoff" else END


def build_triage_graph():
    g = StateGraph(AgentState)
    g.add_node("triage_init", triage_init)
    g.add_node("triage_model", triage_model, retry_policy=RETRY)
    g.add_node("triage_tools", ToolNode(TRIAGE_TOOLS), retry_policy=RETRY)
    g.add_node("triage_classify", triage_classify, retry_policy=RETRY)
    g.add_node("triage_route", triage_route)
    g.add_node("triage_handoff", triage_handoff)
    g.add_edge(START, "triage_init")
    g.add_edge("triage_init", "triage_model")
    g.add_conditional_edges("triage_model", after_model, ["triage_tools", "triage_classify"])
    g.add_edge("triage_tools", "triage_model")
    g.add_edge("triage_classify", "triage_route")
    g.add_conditional_edges("triage_route", after_route, ["triage_handoff", END])
    g.add_edge("triage_handoff", END)
    return g.compile()
