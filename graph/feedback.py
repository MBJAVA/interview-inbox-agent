"""Feedback agent: interprets one piece of feedback (CLI or doc comment) and decides scope.

    feedback_init -> feedback_model --(tool calls, budget left)--> feedback_tools -> feedback_model
                                   \\--(done)--> feedback_classify -> feedback_apply -> END

feedback_apply sets next_step for the parent: "curate" (revision_request / count_change),
"reply" (question / unclear), or "ignore" (note). Feedback text is untrusted data: it can only
change the question list through the curator's guarded tools.
"""
from __future__ import annotations

from typing import Annotated

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode

from core.config import budget, config
from core.llm import get_model, prompt, structured
from graph.common import RETRY, clean_convo, context_block, enforce_tool_budget, reset_messages
from graph.schemas import FeedbackDecision
from graph.state import AgentState
from tools.common import err, ok, untrusted


@tool
def get_question_details(question_id: str, state: Annotated[dict, InjectedState]) -> dict:
    """Get the stored details of one question in the prep doc: prompt, topic, difficulty, difficulty_reason,
    source, source name, link and link status."""
    for q in state.get("questions") or []:
        if q["id"] == question_id:
            return ok(question=q)
    return err("unknown question id", "Use an id from the question list in the context block.")


FEEDBACK_TOOLS = [get_question_details]


def feedback_init(state: AgentState) -> dict:
    fb = state["feedback"]
    interview = state.get("interview") or {}
    ctx = {
        "feedback": untrusted(fb["text"], 1500),
        "feedback_source": fb.get("source"),
        "quoted_text": untrusted(fb.get("quoted_text") or "", 300),
        "interview": {k: interview.get(k) for k in ("company", "role", "level", "round_types", "count_overrides")},
        "questions": [{k: q.get(k) for k in ("id", "title", "round_type", "difficulty", "source")} for q in state.get("questions") or []],
    }
    return {"messages": reset_messages() + [SystemMessage(prompt("feedback")), HumanMessage(context_block(ctx))],
            "step_count": 0, "max_iterations": budget("feedback_max_iterations")}


def feedback_model(state: AgentState) -> dict:
    ai = get_model("feedback").bind_tools(FEEDBACK_TOOLS).invoke(state["messages"])
    ai, upd = enforce_tool_budget(state, ai, "feedback")
    return {"messages": [ai], **upd}


def after_model(state: AgentState) -> str:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None) and state.get("step_count", 0) < state.get("max_iterations", 3):
        return "feedback_tools"
    return "feedback_classify"


def feedback_classify(state: AgentState) -> dict:
    d: FeedbackDecision = structured("feedback_classify", FeedbackDecision).invoke(
        [SystemMessage(prompt("feedback"))] + clean_convo(state["messages"]) + [HumanMessage("Record your decision now.")])
    fb = dict(state["feedback"])
    fb["classification"] = d.classification
    fb["scope"] = d.scope.model_dump()
    return {"feedback": fb, "messages": [HumanMessage("[feedback_decision] " + d.model_dump_json())]}


def _decision(state: AgentState) -> FeedbackDecision:
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage) and isinstance(m.content, str) and m.content.startswith("[feedback_decision] "):
            return FeedbackDecision.model_validate_json(m.content[len("[feedback_decision] "):])
    return FeedbackDecision(classification="unclear", clarifying_question="Could you say what you'd like changed?")


def feedback_apply(state: AgentState) -> dict:
    d = _decision(state)
    fb = dict(state["feedback"])
    interview = dict(state.get("interview") or {})
    rounds_present = interview.get("round_types") or []
    qmap = {q["id"]: q for q in state.get("questions") or []}
    scope = d.scope.model_dump()
    # Normalize scope: question ids imply their rounds; keep only rounds this interview has.
    for qid in scope["question_ids"]:
        if qid in qmap and qmap[qid]["round_type"] not in scope["rounds"]:
            scope["rounds"].append(qmap[qid]["round_type"])
    scope["rounds"] = [r for r in scope["rounds"] if r in rounds_present] or ([] if scope["level"] == "doc" else rounds_present)
    log = [{"tool": "feedback", "outcome": f"classified {d.classification}; scope={scope['level']} {scope['rounds']}", "fallback": None}]

    if d.classification == "revision_request":
        directive = d.directive or "replace with better-fitting questions"
        slot_overrides = {}
        low = directive.lower()
        level = interview.get("level") or "senior"
        mix = config()["slots"].get(level, config()["slots"]["senior"])
        for r in scope["rounds"] or rounds_present:
            if "harder" in low or "too easy" in low:
                slot_overrides[f"{r}:easy"] = [0, 0]
            if "easier" in low or "too hard" in low:
                slot_overrides[f"{r}:hard"] = [0, 0]
                slot_overrides[f"{r}:easy"] = [mix["easy"][1], mix["easy"][1]]
        return {"refine_scope": {**scope, "directive": directive, "slot_overrides": slot_overrides},
                "feedback": fb, "next_step": "curate", "build_log": log}
    if d.classification == "count_change" and d.count_override:
        co = d.count_override
        overrides = dict(interview.get("count_overrides") or {})
        overrides[f"{co.round}:{co.difficulty}"] = int(co.target)
        interview["count_overrides"] = overrides
        return {"interview": interview, "feedback": fb, "next_step": "curate",
                "refine_scope": {"level": "round", "rounds": [co.round], "difficulties": [co.difficulty], "question_ids": [],
                                 "directive": f"set {co.round} {co.difficulty} to {co.target} (stored override)"},
                "build_log": log + [{"tool": "feedback", "outcome": f"stored count override {co.round}:{co.difficulty}={co.target}", "fallback": None}]}
    if d.classification == "question":
        fb["result_line"] = d.answer or "I couldn't find stored details for that question."
        return {"feedback": fb, "next_step": "reply", "build_log": log}
    if d.classification == "note":
        fb["result_line"] = None
        return {"feedback": fb, "next_step": "ignore", "build_log": log}
    fb["classification"] = "unclear"
    fb["result_line"] = d.clarifying_question or "Could you say which round or question you'd like changed, and how?"
    return {"feedback": fb, "next_step": "reply", "build_log": log}


def build_feedback_graph():
    g = StateGraph(AgentState)
    g.add_node("feedback_init", feedback_init)
    g.add_node("feedback_model", feedback_model, retry_policy=RETRY)
    g.add_node("feedback_tools", ToolNode(FEEDBACK_TOOLS))
    g.add_node("feedback_classify", feedback_classify, retry_policy=RETRY)
    g.add_node("feedback_apply", feedback_apply)
    g.add_edge(START, "feedback_init")
    g.add_edge("feedback_init", "feedback_model")
    g.add_conditional_edges("feedback_model", after_model, ["feedback_tools", "feedback_classify"])
    g.add_edge("feedback_tools", "feedback_model")
    g.add_edge("feedback_classify", "feedback_apply")
    g.add_edge("feedback_apply", END)
    return g.compile()

