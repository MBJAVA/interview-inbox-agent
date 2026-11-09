"""Curator state tools: accept_questions / remove_questions.

These are how the curator model changes the question list. They return a LangGraph
Command so the update lands in state, and they enforce every quality rule, the
per-slot cap, link checking, stable difficulty labels, refinement scope, and dedupe
before anything is accepted. The model decides WHAT to propose; these decide whether
it is allowed.
"""
from __future__ import annotations

import json
from typing import Annotated, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field

from core.llm import prompt, structured
from graph import quality


def _normalize_level(v):
    from graph.extraction import normalize_level  # local: extraction imports tools at load

    return normalize_level(v)
from graph.schemas import DifficultyCalibration
from tools.question_bank import bank_by_id, find_bank_match, similar_bank_questions


class Candidate(BaseModel):
    id: str
    round_type: str
    difficulty: str = Field(description="The slot you want to fill: easy, medium, or hard.")
    title: str
    prompt: str
    topic: str = ""
    source: str = Field(description="bank, company_search, or generated")
    source_name: str = ""
    url: Optional[str] = None
    competency: Optional[str] = None
    audience_role: Optional[str] = None
    keep_unverified: bool = Field(False, description="If the link is broken, keep the question without the link, marked unverified.")


def slot_counts(questions: list[dict]) -> dict:
    out: dict = {}
    for q in questions:
        out.setdefault(q["round_type"], {}).setdefault(q["difficulty"], 0)
        out[q["round_type"]][q["difficulty"]] += 1
    return out


def slot_status(questions: list[dict], targets: dict) -> list[dict]:
    counts = slot_counts(questions)
    rows = []
    for rnd, diffs in targets.items():
        for d, (lo, hi) in diffs.items():
            rows.append({"round": rnd, "difficulty": d, "filled": counts.get(rnd, {}).get(d, 0), "min": lo, "max": hi})
    return rows


def in_scope(scope: Optional[dict], round_type: str, qid: Optional[str] = None, for_removal: bool = False) -> bool:
    if not scope:
        return True
    rounds = scope.get("rounds") or []
    if rounds and round_type not in rounds:
        return False
    ids = scope.get("question_ids") or []
    if for_removal and ids and qid not in ids:
        return False
    return True


def calibrate(c: dict, level: str, stored: dict) -> tuple[str, str]:
    if c["id"] in stored:  # stable across rebuilds and refinements
        return stored[c["id"]]["difficulty"], stored[c["id"]]["reason"]
    if c["source"] == "bank" and bank_by_id(c["id"]):
        b = bank_by_id(c["id"])
        diff, reason = b["difficulty"], f"bank label ({b['source_name']}), suited to {', '.join(b['levels'])}"
    else:
        cal = structured("calibration", DifficultyCalibration).invoke([
            SystemMessage(prompt("calibration")),
            HumanMessage(json.dumps({"target_level": level, "requested_difficulty": c["difficulty"],
                                     "round_type": c["round_type"], "title": c["title"], "prompt": c["prompt"][:800],
                                     "topic": c.get("topic"),
                                     "similar_bank_questions": similar_bank_questions(c)})),
        ])
        diff, reason = cal.difficulty, cal.reason
    floored, why = quality.calibration_floor(c, diff)
    if why:
        diff, reason = floored, f"{reason}; {why}"
    return diff, reason


@tool
def accept_questions(candidates: list[Candidate], state: Annotated[dict, InjectedState],
                     tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Propose questions for the prep doc. Each candidate is validated against the quality rules,
    the round's slot cap, the refinement scope, link status (call check_link first for any URL),
    difficulty calibration (labels are stable once assigned), and semantic dedupe across all rounds.
    Returns accepted ids, rejected candidates with reasons, and the current slot counts."""
    current = list(state.get("questions") or [])
    targets = state.get("slot_targets") or {}
    scope = state.get("refine_scope")
    checked = state.get("checked_links") or {}
    stored = dict(state.get("difficulty_labels") or {})
    interview = state.get("interview") or {}
    level = _normalize_level(interview.get("level")) or "senior"
    accepted, rejected, new_labels = [], [], {}
    pool = list(current)
    for cand in candidates:
        c = cand.model_dump() if isinstance(cand, BaseModel) else dict(cand)
        rt = c["round_type"]
        if rt not in targets:
            rejected.append({"id": c["id"], "reason": f"round '{rt}' is not part of this interview"})
            continue
        if not in_scope(scope, rt):
            rejected.append({"id": c["id"], "reason": "outside the refinement scope; that round must stay unchanged"})
            continue
        violations = quality.validate(c)
        if violations:
            rejected.append({"id": c["id"], "reason": "; ".join(violations)})
            continue
        if c["source"] == "generated":
            bm = find_bank_match(c)
            if bm:
                rejected.append({"id": c["id"], "reason": (
                    f"matches bank question '{bm['title']}' (id {bm['id']}, {bm['difficulty']}, {bm['url'] or 'no link'}); "
                    "submit the bank version instead (it keeps the link), or generate a different problem")})
                continue
        link_status = "n/a" if not c.get("url") else "unverified"  # no link at all: nothing to verify
        url = c.get("url")
        if url:
            st = checked.get(url)
            if st is None:
                rejected.append({"id": c["id"], "reason": f"link not checked: call check_link on {url} first"})
                continue
            if st == "broken":
                if not c.get("keep_unverified"):
                    rejected.append({"id": c["id"], "reason": "broken link: find a replacement link, swap in another question, or resubmit with keep_unverified=true"})
                    continue
                url, link_status = None, "unverified"
            else:
                link_status = st
        diff, reason = calibrate(c, level, stored)
        if diff != c["difficulty"]:
            reason = f"{reason} (requested {c['difficulty']}, calibrated {diff})"
        lo_hi = targets[rt].get(diff)
        cap = lo_hi[1] if lo_hi else 0
        filled = sum(1 for q in pool if q["round_type"] == rt and q["difficulty"] == diff)
        if filled >= cap:
            rejected.append({"id": c["id"], "reason": f"{rt}/{diff} is full ({filled}/{cap}); calibrated difficulty: {diff}"})
            new_labels[c["id"]] = {"difficulty": diff, "reason": reason}
            continue
        dup = quality.is_duplicate(c, pool)
        if dup:
            rejected.append({"id": c["id"], "reason": f"duplicate of '{dup['title']}' ({dup['round_type']}); pick a different question"})
            continue
        q = {"id": c["id"], "title": c["title"], "prompt": c["prompt"], "round_type": rt, "topic": c.get("topic", ""),
             "difficulty": diff, "difficulty_reason": reason, "source": c["source"],
             "source_name": c.get("source_name") or c["source"], "url": url, "link_status": link_status,
             "competency": c.get("competency") or (c.get("topic") if rt == "behavioral" else None)}
        accepted.append(q)
        pool.append(q)
        new_labels[c["id"]] = {"difficulty": diff, "reason": reason}
    status = slot_status(pool, targets)
    body = {"ok": True, "accepted": [q["id"] for q in accepted], "rejected": rejected,
            "slots": [s for s in status if s["filled"] < s["max"] or s["filled"] < s["min"]],
            "all_slots_at_min": all(s["filled"] >= s["min"] for s in status)}
    return Command(update={
        "questions": {"add": accepted},
        "difficulty_labels": new_labels,
        "messages": [ToolMessage(json.dumps(body), tool_call_id=tool_call_id, name="accept_questions")],
    })


@tool
def remove_questions(ids: list[str], reason: str, state: Annotated[dict, InjectedState],
                     tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Remove questions from the list (e.g. to swap them during a refinement). Only questions inside the
    refinement scope can be removed. Give a short reason; it is recorded in the build log."""
    current = {q["id"]: q for q in state.get("questions") or []}
    scope = state.get("refine_scope")
    removed, refused = [], []
    for i in ids:
        q = current.get(i)
        if not q:
            refused.append({"id": i, "reason": "not in the list"})
        elif not in_scope(scope, q["round_type"], i, for_removal=True):
            refused.append({"id": i, "reason": "outside the refinement scope"})
        else:
            removed.append(i)
    body = {"ok": True, "removed": removed, "refused": refused}
    return Command(update={
        "questions": {"remove": removed},
        "build_log": [{"tool": "remove_questions", "outcome": f"removed {len(removed)}: {reason}", "fallback": None}] if removed else [],
        "messages": [ToolMessage(json.dumps(body), tool_call_id=tool_call_id, name="remove_questions")],
    })
