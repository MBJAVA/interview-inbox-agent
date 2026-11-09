"""Interview identity (plain code, not an agent).

- Every Gmail thread gets a provisional interview id `gt_<gmail thread id>`; it is also the
  LangGraph thread_id for that email's runs, so interrupts work before the company is known.
- Once company + role are known, the provisional id is linked to the record keyed by
  (company, role). If one exists (e.g. a new thread for the next round), we merge into it.
- Before triage, a Gmail thread that already maps to a record loads that record.
"""
from __future__ import annotations

from core import db

RECORD_KEYS = ["interview", "field_sources", "guessed_fields", "missing_fields", "conflicts", "open_questions",
               "company_process", "research_confidence", "questions", "unfilled_slots", "difficulty_labels",
               "build_log", "revision_history", "pending_handoff"]


def provisional_id(gmail_thread_id: str) -> str:
    return f"gt_{gmail_thread_id}"


def lookup(gmail_thread_id: str) -> dict | None:
    return db.record_for_gmail_thread(gmail_thread_id)


def record_from_state(state: dict) -> dict:
    rec = {"id": state["interview_id"], "gmail_thread_ids": sorted(set(state.get("gmail_thread_ids") or []))}
    for k in RECORD_KEYS:
        if k in state:
            rec[k] = state.get(k)
    rec["build_log"] = (state.get("build_log") or [])[-80:]
    return rec


def state_from_record(rec: dict) -> dict:
    """Load a stored record into graph state (reducer-backed keys use their replace form)."""
    out = {"interview_id": rec["id"], "gmail_thread_ids": rec.get("gmail_thread_ids", []), "existing_record": rec}
    for k in RECORD_KEYS:
        if k not in rec:
            continue
        v = rec[k]
        if k == "build_log":
            out[k] = {"__replace__": v or []}
        elif k in ("field_sources", "difficulty_labels"):
            out[k] = {"__replace__": True, **(v or {})}
        else:
            out[k] = v
    return out


def link(state: dict) -> dict:
    """Link to / merge with the (company, role) record. Returns state updates."""
    interview = state.get("interview") or {}
    company, role = interview.get("company"), interview.get("role")
    current_id = state["interview_id"]
    threads = set(state.get("gmail_thread_ids") or [])
    if not company or not role:
        return {}
    other = db.find_record_by_company_role(company, role)
    if not other:
        other = next((r for r in db.find_records_by_company(company)
                      if not (r.get("interview") or {}).get("role") and r["id"] != current_id
                      and (r.get("interview") or {}).get("status") in ("waiting", "active")), None)
    if not other or other["id"] == current_id:
        return {"build_log": [{"tool": "identity", "outcome": f"record {current_id} keyed by ({company}, {role})", "fallback": None}]}
    # Merge this thread into the existing record (next round / new thread for the same interview).
    merged_interview = dict(other.get("interview") or {})
    for k, v in interview.items():
        if v not in (None, [], {}, "") and k not in ("doc_id", "count_overrides", "status"):
            merged_interview[k] = v
    merged_interview["status"] = interview.get("status", "active")
    if other.get("interview", {}).get("round_types") and (state.get("classification") or {}).get("label") == "next_round":
        merged_interview["round_types"] = list(dict.fromkeys(other["interview"]["round_types"] + (interview.get("round_types") or [])))
    db.delete_record_row(current_id) if db.get_record(current_id) else None
    threads |= set(other.get("gmail_thread_ids") or [])
    for t in threads:
        db.map_thread(t, other["id"])
    sources = {"__replace__": True, **(other.get("field_sources") or {}), **(state.get("field_sources") or {})}
    return {
        "interview_id": other["id"],
        "gmail_thread_ids": sorted(threads),
        "interview": merged_interview,
        "field_sources": sources,
        "questions": other.get("questions") or [],
        "difficulty_labels": {"__replace__": True, **(other.get("difficulty_labels") or {})},
        "revision_history": other.get("revision_history") or [],
        "build_log": [{"tool": "identity", "outcome": f"merged thread record {current_id} into existing record {other['id']} ({company}, {role})", "fallback": None}],
    }
