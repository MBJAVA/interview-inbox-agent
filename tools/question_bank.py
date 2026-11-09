"""search_question_bank: the local, reproducible default source."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Optional

from langchain_core.tools import tool

from core.config import path
from tools.common import ok, safe_tool


@lru_cache(maxsize=1)
def load_bank() -> list[dict]:
    return json.loads(path("question_bank").read_text())


def bank_by_id(qid: str) -> dict | None:
    return next((q for q in load_bank() if q["id"] == qid), None)


def find_bank_match(q: dict) -> dict | None:
    """The bank question a non-bank candidate duplicates (same round, same concept or near-identical title)."""
    from graph import quality

    same_round = [b for b in load_bank() if b["round_type"] == q.get("round_type")]
    return quality.is_duplicate({**q, "id": q.get("id") or ""}, same_round)


def similar_bank_questions(q: dict, limit: int = 4) -> list[dict]:
    """Bank questions in the same round that share title/topic words with q: difficulty reference points."""
    from graph.quality import _tokens

    words = _tokens(f"{q.get('title', '')} {(q.get('topic') or '').replace('/', ' ')}")
    scored = []
    for b in load_bank():
        if b["round_type"] != q.get("round_type"):
            continue
        overlap = len(words & _tokens(f"{b['title']} {b['topic'].replace('/', ' ')}"))
        if overlap:
            scored.append((overlap, b))
    scored.sort(key=lambda x: -x[0])
    return [{"title": b["title"], "difficulty": b["difficulty"], "source": b["source_name"]} for _, b in scored[:limit]]


@tool
@safe_tool("Loosen the filters (drop topic, then difficulty) or use another source.")
def search_question_bank(round_type: str, level: Optional[str] = None, difficulty: Optional[str] = None,
                         topic: Optional[str] = None, exclude_ids: Optional[list[str]] = None, limit: int = 6) -> dict:
    """Search the local question bank (curated from NeetCode/Blind 75, Grind 75, system-design-primer,
    awesome-low-level-design, Tech Interview Handbook). Filter by round_type (coding, system_design,
    low_level_design, behavioral), level, difficulty (easy/medium/hard), topic substring; exclude ids already
    used. There is no bank for ai_assisted_coding."""
    if round_type == "ai_assisted_coding":
        return ok(results=[], count=0, note="No public bank for ai_assisted_coding; use generate_tailored_question.")
    exclude = set(exclude_ids or [])

    def matches(q: dict, use_topic: bool) -> bool:
        if q["round_type"] != round_type or q["id"] in exclude:
            return False
        if difficulty and q["difficulty"] != difficulty:
            return False
        hay = (q["topic"] + " " + q["title"] + " " + " ".join(q["industry_tags"])).lower()
        return not (use_topic and topic) or any(w in hay for w in _topic_words(topic))

    res = [q for q in load_bank() if matches(q, True)]
    relaxed = bool(topic) and not res
    if relaxed:  # a narrow topic shouldn't cost a wasted call: fall back to the round + difficulty
        res = [q for q in load_bank() if matches(q, False)]
    # Prefer questions tagged for the level.
    if level:
        res.sort(key=lambda q: 0 if level in q["levels"] else 1)
    res = res[: max(1, min(int(limit), 15))]
    note = None if res else "Bank has nothing for this round/difficulty. Use generation (or company search)."
    if relaxed and res:
        note = (f"Nothing matched topic '{topic}'; these are OFF-TOPIC {round_type} questions at this difficulty. "
                "If the topic is one of the interview's focus topics, generate on-topic questions instead.")
    return ok(results=[{**q, "source": "bank"} for q in res], count=len(res), note=note)


def _topic_words(topic: str) -> list[str]:
    """'graphs/bfs' or 'arrays, strings' -> match on any of the parts, not the whole phrase."""
    parts = [p.strip().lower() for p in re.split(r"[/,;|]| and ", topic) if p.strip()]
    return parts or [topic.lower()]
