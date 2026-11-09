"""Curator agent (non-interactive) with the coverage loop.

    curator_init -> curator_model --(tool calls, step budget left)--> curator_tools -> curator_model
                                 \\--(stops / step budget hit)--> coverage_check
    coverage_check --(gaps and iterations left)--> curator_model   (gaps written into the conversation)
                   \\--(meets goal / budget hit / no progress)--> curator_finalize -> END

The model chooses the source per round and slot (bank, company search, generation),
handles broken links, and reacts to accept_questions rejections. It never interrupts:
whatever is unresolved goes into unfilled_slots and the doc's Open questions.
"""
from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from core.config import budget, config
from core.llm import get_model, prompt, structured
from graph import quality


def _normalize_level(v):
    from graph.extraction import normalize_level  # local: extraction imports tools at load

    return normalize_level(v)
from graph.common import RETRY, context_block, enforce_tool_budget, log_tool_failures, reset_messages
from graph.schemas import CoverageReport
from graph.state import AgentState
from tools.company_questions import search_company_questions
from tools.curation import accept_questions, remove_questions, slot_counts, slot_status
from tools.generator import generate_tailored_question
from tools.links import check_link
from tools.question_bank import search_question_bank

CURATOR_TOOLS = [search_question_bank, search_company_questions, generate_tailored_question, check_link,
                 accept_questions, remove_questions]


def compute_targets(interview: dict, scope: dict | None = None) -> dict:
    from graph.extraction import normalize_level

    level = normalize_level(interview.get("level")) or config()["extraction"]["default_level"]
    mix = config()["slots"].get(level) or config()["slots"]["senior"]
    overrides = interview.get("count_overrides") or {}
    temp = (scope or {}).get("slot_overrides") or {}
    targets = {}
    for rnd in interview.get("round_types") or []:
        targets[rnd] = {}
        for diff, (lo, hi) in mix.items():
            key = f"{rnd}:{diff}"
            if key in overrides:
                lo = hi = int(overrides[key])
            if key in temp:
                lo, hi = temp[key]
            targets[rnd][diff] = [int(lo), int(hi)]
    return targets


def recheck_link(q: dict) -> dict:
    """Kept questions whose link couldn't be verified last time get one more check (cheap, deterministic)."""
    if q.get("url") and q.get("link_status") == "unverified":
        from tools.web import link_status

        try:
            return {**q, "link_status": link_status(q["url"])}
        except Exception:
            return q
    if not q.get("url") and q.get("link_status") == "unverified" and q.get("source") == "bank":
        from tools.question_bank import bank_by_id

        if not (bank_by_id(q["id"]) or {}).get("url"):
            return {**q, "link_status": "n/a"}  # never had a link (vs. one that broke and was dropped)
    return q


def curator_context(state: dict, targets: dict, current: list[dict]) -> dict:
    """Everything the curator needs to know, rebuilt from state (so each coverage pass can start fresh)."""
    interview = state["interview"]
    scope = state.get("refine_scope")
    level = _normalize_level(interview.get("level")) or "senior"
    return {
        "interview": {k: interview.get(k) for k in ("company", "role", "level", "round_types", "industry", "focus_topics",
                                                   "job_post_summary", "interview_datetime")},
        "slot_targets": targets,
        "fill_order": config()["fill_order"].get(level, ["hard", "medium", "easy"]),
        "current_questions": [{k: q.get(k) for k in ("id", "title", "round_type", "difficulty", "topic", "source")} for q in current],
        "current_slot_status": slot_status(current, targets),
        "refinement": {"scope": scope, "feedback": (state.get("feedback") or {}).get("text")} if scope else None,
        "count_overrides": interview.get("count_overrides") or {},
    }


def curator_init(state: AgentState) -> dict:
    interview = state["interview"]
    scope = state.get("refine_scope")
    targets = compute_targets(interview, scope)
    # Questions for rounds that are no longer part of the interview are dropped (e.g. round changed).
    current = [q for q in (state.get("questions") or []) if q["round_type"] in targets]
    dropped = len(state.get("questions") or []) - len(current)
    current = [recheck_link(q) for q in current]  # e.g. LeetCode links stored as unverified before the lookup fix
    ctx = curator_context(state, targets, current)
    # Budget scales with the work: a 4-round staff loop needs far more calls than one coding screen.
    slots = sum(hi for diffs in targets.values() for _, hi in diffs.values())
    used = state.get("tool_call_count") or 0
    need = min(budget("max_tool_calls"), used + 10 + budget("curator_tool_calls_per_slot") * slots)
    upd = {
        "tool_call_budget": max(state.get("tool_call_budget") or 0, need),
        "messages": reset_messages() + [SystemMessage(prompt("curator")), HumanMessage(context_block(ctx))],
        "slot_targets": targets,
        "iteration_count": 0,
        "step_count": 0,
        "max_iterations": budget("curator_max_iterations"),
        "coverage_gaps": [],
        "questions": current,
        "questions_before": current,
        "last_question_count": -1,
        "last_question_ids": None,
    }
    if dropped:
        upd["build_log"] = [{"tool": "curator.init", "outcome": f"dropped {dropped} question(s) for rounds no longer in the interview", "fallback": None}]
    return upd


def curator_model(state: AgentState) -> dict:
    model = get_model("curator").bind_tools(CURATOR_TOOLS)
    failures = log_tool_failures(state["messages"], "curator")
    ai = model.invoke(state["messages"])
    ai, upd = enforce_tool_budget(state, ai, "curator")
    upd["build_log"] = failures + upd.get("build_log", [])
    return {"messages": [ai], **upd}


def after_model(state: AgentState) -> str:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None) and state.get("step_count", 0) < budget("curator_max_model_steps"):
        return "curator_tools"
    return "coverage_check"


def coverage_check(state: AgentState) -> dict:
    questions = state.get("questions") or []
    targets = state["slot_targets"]
    status = slot_status(questions, targets)
    # Deterministic facts first; the LLM judges quality and decides which gaps are worth another pass.
    code_gaps = [s for s in status if s["filled"] < s["min"]]
    violations = [f"{q['id']}: {v}" for q in questions for v in quality.validate(q)]
    report: CoverageReport = structured("coverage", CoverageReport).invoke([
        SystemMessage(prompt("coverage")),
        HumanMessage(context_block({"slot_status": status,
            "focus_topics": (state.get("interview") or {}).get("focus_topics") or [],
            "questions": [
            {k: q.get(k) for k in ("id", "title", "round_type", "difficulty", "topic", "source", "link_status")} for q in questions],
            "rule_violations_found_by_validator": violations})),
    ])
    gaps = [f"{s['round']}: {s['filled']}/{s['min']} {s['difficulty']}" for s in code_gaps]
    gaps += [f"{g.round}: {g.filled}/{g.target} {g.difficulty}" for g in report.gaps
             if f"{g.round}: {g.filled}/{g.target} {g.difficulty}" not in gaps]
    meets = report.meets_goal and not code_gaps and not violations
    it = (state.get("iteration_count") or 0) + 1
    entry = {"tool": "coverage_check", "outcome": f"iteration {it}: {'goal met' if meets else 'gaps: ' + ', '.join(gaps) or 'quality issues'}",
             "fallback": None}
    ids = sorted(q["id"] for q in questions)
    upd = {"iteration_count": it, "coverage_gaps": gaps if not meets else [], "build_log": [entry], "step_count": 0,
           "last_question_count": len(questions), "last_question_ids": ids}
    if not meets and it < state.get("max_iterations", 5):
        made_progress = state.get("last_question_ids") != ids  # a swap changes ids, not the count
        if not made_progress and it > 1:
            upd["coverage_gaps"] = gaps
            upd["build_log"] = [entry, {"tool": "coverage_check", "outcome": "no progress in the last iteration", "fallback": "stopped; gaps reported"}]
            upd["next_step"] = "finalize"
            return upd
        # Each pass starts a fresh conversation: instructions + current list + gaps. Re-sending the whole
        # history every turn made tokens grow quadratically on big loops (and left cut-off tool calls behind).
        upd["messages"] = reset_messages() + [
            SystemMessage(prompt("curator")), HumanMessage(context_block(curator_context(state, targets, questions))),
            HumanMessage(
            "Coverage check: the goal is not met yet. Gaps: " + "; ".join(gaps or ["see quality issues"])
            + (". Quality issues: " + "; ".join(report.quality_issues + violations) if (report.quality_issues or violations) else "")
            + ". Fill these slots (try another source if one came up empty). For a quality issue in a full slot, "
            "swap: remove_questions on the weakest off-topic question(s), then accept on-topic replacements. Then stop.")]
        upd["next_step"] = "loop"
    else:
        if not meets:
            upd["budget_exhausted"] = list(state.get("budget_exhausted") or []) + [f"curator: max_iterations ({state.get('max_iterations')}) reached with gaps"]
        upd["next_step"] = "finalize"
    return upd


def after_coverage(state: AgentState) -> str:
    return "curator_model" if state.get("next_step") == "loop" else "curator_finalize"


def curator_finalize(state: AgentState) -> dict:
    """Deterministic safety net: trim anything over cap or duplicated (parallel tool calls can race),
    then compute unfilled slots from the final list."""
    targets = state["slot_targets"]
    kept, removed = [], []
    for q in state.get("questions") or []:
        lo_hi = targets.get(q["round_type"], {}).get(q["difficulty"])
        cap = lo_hi[1] if lo_hi else 0
        n = sum(1 for k in kept if k["round_type"] == q["round_type"] and k["difficulty"] == q["difficulty"])
        if n >= cap or quality.is_duplicate(q, kept) or quality.validate(q) or q.get("link_status") == "broken":
            removed.append(q["id"])
            continue
        kept.append(q)
    unfilled = [{"round": s["round"], "difficulty": s["difficulty"], "filled": s["filled"], "target": s["min"]}
                for s in slot_status(kept, targets) if s["filled"] < s["min"]]
    log = [{"tool": "curator.finalize", "outcome": f"{len(kept)} questions; {len(unfilled)} unfilled slot(s)", "fallback": None}]
    if removed:
        log.append({"tool": "curator.finalize", "outcome": f"trimmed {len(removed)} over-cap/duplicate question(s)", "fallback": None})
    upd = {"questions": kept, "unfilled_slots": unfilled, "build_log": log}
    scope = state.get("refine_scope")
    if scope:
        before = {q["id"] for q in (state.get("questions_before") or [])}
        after = {q["id"] for q in kept}
        added, dropped = after - before, before - after
        counts = slot_counts([q for q in kept if q["round_type"] in (scope.get("rounds") or targets.keys())])
        upd["revision_note"] = (f"Refined {', '.join(scope.get('rounds') or ['all rounds'])}: "
                                f"swapped out {len(dropped)}, added {len(added)}"
                                + (f" ({scope.get('directive')})" if scope.get("directive") else "")
                                + f"; now {json.dumps(counts)}")
    return upd


def build_curator_graph():
    g = StateGraph(AgentState)
    g.add_node("curator_init", curator_init)
    g.add_node("curator_model", curator_model, retry_policy=RETRY)
    g.add_node("curator_tools", ToolNode(CURATOR_TOOLS), retry_policy=RETRY)
    g.add_node("coverage_check", coverage_check, retry_policy=RETRY)
    g.add_node("curator_finalize", curator_finalize)
    g.add_edge(START, "curator_init")
    g.add_edge("curator_init", "curator_model")
    g.add_conditional_edges("curator_model", after_model, ["curator_tools", "coverage_check"])
    g.add_edge("curator_tools", "curator_model")
    g.add_conditional_edges("coverage_check", after_coverage, ["curator_model", "curator_finalize"])
    g.add_edge("curator_finalize", END)
    return g.compile()
