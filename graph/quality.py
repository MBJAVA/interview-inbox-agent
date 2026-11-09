"""Deterministic quality rules applied before any question is accepted, plus semantic
dedupe and the never-easy calibration floor. The LLM proposes; these rules enforce."""
from __future__ import annotations

import re
from urllib.parse import urlparse

COMPETENCIES = ["conflict", "ownership", "failure", "influence", "leadership", "ambiguity"]

CHEATSHEET_HINTS = ["cheatsheet", "cheat-sheet", "cheat sheet", "/blind75", "/study-plan", "/explore/", "patterns",
                    "/list/", "/problemset", "github.com/seanprashad"]
CONCEPT_PATTERNS = [r"^\s*explain\b", r"^\s*what is\b", r"^\s*describe the difference", r"\btrade-?off between\b",
                    r"^\s*compare\b", r"^\s*define\b"]
OTHER_ROLE_MARKERS = ["product manager", "product-manager", "/product-manager", "pm interview", "pm candidates",
                      "data scientist", "data-scientist", "ux designer", "product design interview", "program manager"]
AI_ASSIST_WORDS = ["ai assistant", "ai coding assistant", "copilot", "llm", "ai-generated", "ai pair", "assistant"]
AI_VERIFY_WORDS = ["verify", "review", "inspect", "test", "run", "correct", "fix"]

# Topics that are never "easy" for any level (difficulty calibration floor).
NEVER_EASY = ["real-time collaborative", "collaborative editing", "distributed tracing", "consensus", "crdt",
              "exactly-once", "order matching", "ledger", "distributed transaction", "geo-replicat"]

SYNONYMS = {
    "rate limiter": "ratelimit", "rate limiting": "ratelimit", "token bucket": "ratelimit", "throttl": "ratelimit",
    "lru": "lrucache", "url shortener": "urlshort", "pastebin": "urlshort", "tinyurl": "urlshort", "bit.ly": "urlshort",
    "chat": "chat", "whatsapp": "chat", "messaging": "chat", "news feed": "feed", "timeline": "feed",
    "collaborative edit": "collab", "google docs": "collab", "ledger": "ledger", "payout": "payout",
    "notification": "notify", "web crawler": "crawler", "parking lot": "parking", "elevator": "elevator",
    "key-value store": "kvstore", "key value store": "kvstore", "order book": "orderbook", "order-matching": "orderbook",
    "order matching": "orderbook", "tracing": "tracing", "ride-hailing": "ridehail", "uber": "ridehail",
}
STOP = set("a an the of for and or with to in on design implement build given return input output constraints "
           "your you that this is are be by from as at it its using use system service class".split())


_PREFIX_KEYS = {"throttl", "collaborative edit", "notification", "elevator"}
_SYN_RE = {k: re.compile(r"\b" + re.escape(k) + ("" if k in _PREFIX_KEYS else r"(?![a-z])")) for k in SYNONYMS}


def canonical_keys(q: dict) -> set[str]:
    text = f"{q.get('title', '')} {q.get('topic', '')}".lower()
    return {SYNONYMS[k] for k, rx in _SYN_RE.items() if rx.search(text)}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 2}


def is_duplicate(q: dict, others: list[dict]) -> dict | None:
    """Semantic dedupe across all rounds: canonical concept match or high title-token overlap."""
    keys = canonical_keys(q)
    tq = _tokens(q.get("title", ""))
    for o in others:
        if o["id"] == q["id"]:
            return o
        if keys and keys & canonical_keys(o):
            return o
        to = _tokens(o.get("title", ""))
        if tq and to and len(tq & to) / len(tq | to) >= 0.6:
            return o
    return None


def calibration_floor(q: dict, difficulty: str) -> tuple[str, str | None]:
    text = f"{q.get('title', '')} {q.get('topic', '')} {q.get('prompt', '')}".lower()
    if difficulty == "easy" and any(k in text for k in NEVER_EASY):
        return "medium", "raised from easy: this topic is never easy"
    return difficulty, None


def validate(q: dict, target_role: str = "software_engineer") -> list[str]:
    """Return a list of violations (empty = acceptable)."""
    v: list[str] = []
    rt = q.get("round_type")
    title, prompt = q.get("title", ""), q.get("prompt", "")
    text = f"{title} {prompt}".lower()
    url = (q.get("url") or "").lower()
    if not title or not prompt:
        v.append("missing title or prompt")
    audience = (q.get("audience_role") or "software_engineer").lower()
    if audience not in ("software_engineer", "software engineer", "swe", "engineer", target_role):
        v.append(f"role filter: source targets '{audience}', not a software engineer")
    if any(m in text or m in url or m in (q.get("source_name") or "").lower() for m in OTHER_ROLE_MARKERS):
        v.append("role filter: source is aimed at another role")
    if rt == "coding":
        if any(re.search(p, prompt.lower()) for p in CONCEPT_PATTERNS) or any(re.search(p, title.lower()) for p in CONCEPT_PATTERNS):
            v.append("coding: concept question, not a coding problem")
        has_io = ("input" in text or "given" in text) and ("output" in text or "return" in text)
        if not has_io:
            v.append("coding: must state input and output")
        if "constraint" not in text and not re.search(r"\b(o\(|up to|10\^|at most|<=)", text):
            v.append("coding: must state constraints")
        if url and any(h in url for h in CHEATSHEET_HINTS):
            v.append("coding: link must point to the problem itself, not a cheatsheet/list")
        if q.get("source") == "bank" and url and "leetcode.com/problems/" not in url:
            v.append("coding: bank questions must link directly to the problem page")
    elif rt == "ai_assisted_coding":
        if not any(w in text for w in AI_ASSIST_WORDS):
            v.append("ai_assisted_coding: must involve using an AI coding assistant")
        if sum(1 for w in AI_VERIFY_WORDS if w in text) < 2:
            v.append("ai_assisted_coding: must require inspecting, running, verifying and correcting the AI output")
        if re.match(r"^\s*design (a|an|the)\b", title.lower()) and "assistant" not in title.lower():
            v.append("ai_assisted_coding: plain system design prompt is not allowed")
    elif rt in ("system_design", "low_level_design"):
        if not re.search(r"\b(design|architect|build|implement|model)\s+(a|an|the|classes|your|its?|how)\b", text):
            v.append(f"{rt}: must be a design prompt (e.g. 'Design a ...')")
    elif rt == "behavioral":
        comp = (q.get("competency") or q.get("topic") or "").lower()
        if not any(c in comp for c in COMPETENCIES):
            v.append("behavioral: must map to a competency (conflict, ownership, failure, influence, leadership, ambiguity)")
    else:
        v.append(f"unknown round type {rt}")
    if url and not urlparse(url).scheme.startswith("http"):
        v.append("url must be http(s)")
    return v
