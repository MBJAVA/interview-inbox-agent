"""Prep doc content (sections 1-6). Section 7 "My notes" belongs to the user and is never written.
All counts in "How this was built" are computed from the final list."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from graph.state import ROUND_TYPES

ROUND_NAMES = {"coding": "Coding", "ai_assisted_coding": "AI-assisted coding", "system_design": "System design",
               "low_level_design": "Low-level design", "behavioral": "Behavioral"}
FIELDS = [("company", "Company"), ("role", "Role"), ("level", "Level"), ("round_types", "Round(s)"),
          ("interview_datetime", "Date/time"), ("format", "Format"), ("job_post", "Job post"),
          ("recruiter_name", "Recruiter"), ("industry", "Industry"), ("focus_topics", "Focus topics"), ("notes", "Notes"),
          ("status", "Status")]


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "TBD"
    from core.clock import user_tz

    dt = datetime.fromisoformat(iso).astimezone(user_tz())
    return dt.strftime("%a %b %-d, %Y %-I:%M %p ") + (dt.tzname() or "")


def doc_title(interview: dict) -> str:
    rounds = ", ".join(ROUND_NAMES.get(r, r) for r in interview.get("round_types") or []) or "Rounds TBD"
    date = datetime.fromisoformat(interview["interview_datetime"]).strftime("%Y-%m-%d") if interview.get("interview_datetime") else "Date TBD"
    return f"{interview.get('company') or 'Unknown company'} – {interview.get('role') or 'Role TBD'} – {rounds} – {date}"


SOURCE_LABELS = {"generated": "Generated for this interview", "company_search": "Candidate-reported"}


def _reason(text: str) -> str:
    """The calibration reason without the internal '(requested x, calibrated y)' / bank-label boilerplate."""
    text = re.sub(r"\s*\(requested \w+, calibrated \w+\)", "", text or "").strip()
    return "" if text.startswith("bank label") else text


def question_block(q: dict) -> str:
    """One question: bold title (+ link), the prompt, then a short details line."""
    head = f"- **{q['title']}**"
    if q.get("url"):
        head += f" — [{'Open on LeetCode' if 'leetcode.com' in q['url'] else 'Open problem'}]({q['url']})"
    source = q.get("source_name") if q["source"] == "bank" else SOURCE_LABELS.get(q["source"], q.get("source_name"))
    details = [f"Topic: {q['topic']}" if q.get("topic") else None, f"Source: {source}" if source else None]
    if q.get("link_status") in ("unverified", "broken"):
        details.append("link not verified")
    lines = [head, f"  - {q['prompt'].strip()}", "  - _" + " · ".join(d for d in details if d) + "_"]
    why = _reason(q.get("difficulty_reason", ""))
    if why:
        lines.append(f"  - _Why {q['difficulty']}: {why}_")
    return "\n".join(lines)


def build_sections(state: dict) -> list[tuple[str, str]]:
    i = state.get("interview") or {}
    sources = state.get("field_sources") or {}
    guessed = set(state.get("guessed_fields") or [])
    questions = state.get("questions") or []

    # 1. Interview details
    lines = []
    for key, label in FIELDS:
        val = i.get(key)
        if key == "interview_datetime":
            val = fmt_dt(val) if val else None
        if key == "round_types" and val:
            val = ", ".join(ROUND_NAMES.get(r, r) for r in val)
        if isinstance(val, list):
            val = ", ".join(val)
        if not val:
            continue
        if key == "level":
            from graph.extraction import normalize_level

            val = normalize_level(val) or val
        mark = " **(guessed)**" if key in guessed else ""
        src = (sources.get(key) or "").replace("existing_interview_record", "earlier email").replace("_", " ") \
            .replace("America/Los Angeles", "Pacific time").replace("America/New York", "Eastern time") or None
        lines.append(f"- **{label}:** {val}{mark}" + (f"  _(source: {src})_" if src else ""))
    if i.get("count_overrides"):
        lines.append(f"- **Count overrides:** " + ", ".join(f"{k} = {v}" for k, v in i["count_overrides"].items()))
    details = "\n".join(lines)

    # 2. Research confidence
    rc = state.get("research_confidence")
    if rc:
        conf = f"**{(rc.get('level') or 'low').capitalize()}:** {rc.get('reason') or ''}"
        if rc.get("sources"):
            conf += "\n\nSources: " + ", ".join(rc["sources"][:5])
    elif "round_types" in sources and "research" not in sources.get("round_types", ""):
        conf = "**High:** round types are stated in the email(s); no research needed."
    else:
        conf = "**n/a:** no company research was needed for this interview."

    # 3. Questions grouped by round, then difficulty
    qparts = []
    for rnd in ROUND_TYPES:
        rq = [q for q in questions if q["round_type"] == rnd]
        if not rq and rnd not in (i.get("round_types") or []):
            continue
        qparts.append(f"### {ROUND_NAMES[rnd]}")
        for d in ["hard", "medium", "easy"]:
            dq = [q for q in rq if q["difficulty"] == d]
            if not dq:
                continue
            qparts.append(f"#### {d.capitalize()}")
            for q in dq:
                qparts.append(question_block(q))
        if not rq:
            qparts.append("_No questions yet; see Open questions._")
        qparts.append("")
    qtext = "\n".join(qparts).strip() or "_No questions (curation skipped)._"

    # 4. Open questions
    oq = []
    for s in state.get("unfilled_slots") or []:
        oq.append(f"- Not enough questions found for {ROUND_NAMES.get(s['round'], s['round'])} ({s['difficulty']}): "
                  f"{s['filled']} of {s['target']}")
    for g in sorted(guessed):
        oq.append(f"- Guessed field: {g} (please confirm)")
    for q in state.get("open_questions") or []:
        oq.append(f"- {q}")
    for c in state.get("conflicts") or []:
        oq.append(f"- Conflict: {c}")
    ph = state.get("pending_handoff")
    if ph and ph.get("question") and ph.get("status") != "timed_out" and not any(ph["question"] in x for x in oq):
        oq.append(f"- Waiting on you: {ph['question']}")
    for b in state.get("budget_exhausted") or []:
        oq.append(f"- Budget limit: {b}")
    open_text = "\n".join(dict.fromkeys(oq)) or "None."

    # 5. How this was built (counts computed from the final list)
    src_counts = Counter(q["source"] for q in questions)
    link_counts = Counter(q.get("link_status") for q in questions)
    slot_counts = Counter((q["round_type"], q["difficulty"]) for q in questions)
    log = state.get("build_log") or []
    hb = [f"- Questions: {len(questions)} total — " + ", ".join(f"{k}: {v}" for k, v in sorted(src_counts.items())),
          "- Per slot: " + ", ".join(f"{ROUND_NAMES.get(r, r)} {d}: {n}" for (r, d), n in sorted(slot_counts.items())),
          "- Link status: " + ", ".join(f"{k}: {v}" for k, v in sorted(link_counts.items(), key=str)),
          f"- Coverage iterations (last curation): {state.get('iteration_count') or 0}",
          f"- Tool calls (last run): {state.get('tool_call_count') or 0}"]
    hb.append("- Field sources: " + "; ".join(f"{k}: {v}" for k, v in sources.items()))
    # Failures/recoveries carry a fallback (or a "failed: ..." outcome); plain decisions don't.
    failures = [e for e in log if (e.get("outcome") or "").startswith("failed") or e.get("fallback")]
    if failures:
        hb.append("- Tool failures and recoveries:")
        hb += [f"  - {e['tool']}: {e['outcome']}" + (f" → {e['fallback']}" if e.get("fallback") else "") for e in failures[-12:]]
    if state.get("budget_exhausted"):
        hb.append("- Budget limits hit: " + "; ".join(state["budget_exhausted"]))
    steps = [e for e in log if e not in failures][-10:]
    if steps:
        hb.append("- Recent steps:")
        hb += [f"  - {e['tool']}: {e['outcome']}" for e in steps]
    built = "\n".join(hb)

    # 6. Revision history
    rh = "\n".join(f"- {r['date']}: {r['note']}" for r in state.get("revision_history") or []) or "- (first version)"

    return [("Interview details", details), ("Research confidence", conf), ("Questions", qtext),
            ("Open questions", open_text), ("How this was built", built), ("Revision history", rh)]

