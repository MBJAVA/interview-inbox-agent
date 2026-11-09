"""Interview record tools (SQLite)."""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool

from core import db
from tools.common import err, ok, safe_tool


def summarize_record(r: dict) -> dict:
    i = r.get("interview", {})
    return {"interview_id": r["id"], "company": i.get("company"), "role": i.get("role"), "level": i.get("level"),
            "round_types": i.get("round_types"), "interview_datetime": i.get("interview_datetime"),
            "status": i.get("status"), "doc_id": i.get("doc_id"), "gmail_thread_ids": r.get("gmail_thread_ids", []),
            "question_count": len(r.get("questions", []))}


@tool
@safe_tool("Pass interview_id, gmail_thread_id, or company (+ role).")
def get_interview_record(interview_id: Optional[str] = None, gmail_thread_id: Optional[str] = None,
                         company: Optional[str] = None, role: Optional[str] = None) -> dict:
    """Look up an existing interview record by interview id, Gmail thread id, or (company, role).
    With only company, returns every record for that company. Use it to tell whether this email is about
    an interview already being tracked (follow-up, reschedule, next round)."""
    if interview_id:
        r = db.get_record(interview_id)
        return ok(record=summarize_record(r)) if r else ok(record=None)
    if gmail_thread_id:
        r = db.record_for_gmail_thread(gmail_thread_id)
        return ok(record=summarize_record(r) if r else None)
    if company and role:
        r = db.find_record_by_company_role(company, role)
        if r:
            return ok(record=summarize_record(r))
    if company:
        return ok(records=[summarize_record(r) for r in db.find_records_by_company(company)])
    return err("no lookup key given", "Pass interview_id, gmail_thread_id, or company (+ role).")


@tool
@safe_tool("Record must include 'id' and 'interview'.")
def save_interview_record(record: dict) -> dict:
    """Save (upsert) an interview record. Graph nodes persist records; agents normally don't call this."""
    if "id" not in record or "interview" not in record:
        return err("invalid record", "Record must include 'id' and 'interview'.")
    db.save_record(record)
    return ok(interview_id=record["id"])
