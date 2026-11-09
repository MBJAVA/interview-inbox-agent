"""SQLite store for durable agent records (separate from the LangGraph checkpointer).

Tables
- records:            the interview record (source of truth across threads and days)
- thread_map:         gmail_thread_id -> record_id
- processed_messages: idempotency for the poller
- pending_handoffs:   interrupts waiting on the user (survive restarts)
- processed_comments: doc comment IDs already handled
- research_cache:     research_company_process cache by (company, role)
- meta:               last run timestamp etc.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from core import clock
from core.config import path

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, company_key TEXT, role_key TEXT, status TEXT,
  data TEXT NOT NULL, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS records_company_role ON records(company_key, role_key);
CREATE TABLE IF NOT EXISTS thread_map (gmail_thread_id TEXT PRIMARY KEY, record_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed_messages (
  message_id TEXT PRIMARY KEY, thread_id TEXT, record_id TEXT, outcome TEXT, processed_at TEXT);
CREATE TABLE IF NOT EXISTS pending_handoffs (
  graph_thread_id TEXT PRIMARY KEY, record_id TEXT, message_id TEXT, question TEXT,
  payload TEXT, created_at TEXT, timeout_at TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS processed_comments (comment_id TEXT PRIMARY KEY, doc_id TEXT, outcome TEXT, processed_at TEXT);
CREATE TABLE IF NOT EXISTS research_cache (key TEXT PRIMARY KEY, data TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def conn() -> sqlite3.Connection:
    global _conn, _conn_path
    p = str(path("db"))
    with _lock:
        if _conn is None or _conn_path != p:
            _conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn_path = p
        return _conn


def reset_connection() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def norm(s: str | None) -> str:
    return " ".join((s or "").lower().replace(",", " ").split())


def _now() -> str:
    return clock.now().isoformat()


# ---------------------------------------------------------------- records
def get_record(record_id: str) -> dict | None:
    row = conn().execute("SELECT data FROM records WHERE id=?", (record_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def find_record_by_company_role(company: str, role: str) -> dict | None:
    row = conn().execute(
        "SELECT data FROM records WHERE company_key=? AND role_key=? ORDER BY created_at LIMIT 1",
        (norm(company), norm(role)),
    ).fetchone()
    return json.loads(row["data"]) if row else None


def find_records_by_company(company: str) -> list[dict]:
    rows = conn().execute("SELECT data FROM records WHERE company_key=?", (norm(company),)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def record_for_gmail_thread(gmail_thread_id: str) -> dict | None:
    row = conn().execute("SELECT record_id FROM thread_map WHERE gmail_thread_id=?", (gmail_thread_id,)).fetchone()
    return get_record(row["record_id"]) if row else None


def save_record(record: dict) -> None:
    rid = record["id"]
    interview = record.get("interview", {})
    existing = conn().execute("SELECT created_at FROM records WHERE id=?", (rid,)).fetchone()
    created = existing["created_at"] if existing else _now()
    with _lock:
        conn().execute(
            "INSERT OR REPLACE INTO records(id, company_key, role_key, status, data, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (rid, norm(interview.get("company")), norm(interview.get("role")), interview.get("status", "active"),
             json.dumps(record, default=str), created, _now()),
        )
        for t in record.get("gmail_thread_ids", []):
            conn().execute("INSERT OR REPLACE INTO thread_map(gmail_thread_id, record_id) VALUES(?,?)", (t, rid))


def map_thread(gmail_thread_id: str, record_id: str) -> None:
    conn().execute("INSERT OR REPLACE INTO thread_map(gmail_thread_id, record_id) VALUES(?,?)", (gmail_thread_id, record_id))


def delete_record_row(record_id: str) -> None:
    """Only used when a provisional record is merged into an existing one (no user data is lost)."""
    conn().execute("DELETE FROM records WHERE id=?", (record_id,))


def list_records() -> list[dict]:
    rows = conn().execute("SELECT data FROM records ORDER BY created_at").fetchall()
    return [json.loads(r["data"]) for r in rows]


# ---------------------------------------------------------------- processed messages
def is_processed(message_id: str) -> bool:
    return conn().execute("SELECT 1 FROM processed_messages WHERE message_id=?", (message_id,)).fetchone() is not None


def mark_processed(message_id: str, thread_id: str, record_id: str | None, outcome: str) -> None:
    conn().execute(
        "INSERT OR REPLACE INTO processed_messages(message_id, thread_id, record_id, outcome, processed_at) VALUES(?,?,?,?,?)",
        (message_id, thread_id, record_id, outcome, _now()),
    )


def processed_outcomes() -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM processed_messages ORDER BY processed_at").fetchall()]


# ---------------------------------------------------------------- handoffs
def add_pending(graph_thread_id: str, record_id: str, message_id: str, question: str, payload: dict,
                created_at: str, timeout_at: str) -> None:
    conn().execute(
        "INSERT OR REPLACE INTO pending_handoffs VALUES(?,?,?,?,?,?,?,?)",
        (graph_thread_id, record_id, message_id, question, json.dumps(payload, default=str), created_at, timeout_at, "pending"),
    )


def list_pending(status: str = "pending") -> list[dict]:
    rows = conn().execute("SELECT * FROM pending_handoffs WHERE status=? ORDER BY created_at", (status,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"] or "{}")
        out.append(d)
    return out


def find_handoff(interview_id: str) -> dict | None:
    for status in ("pending", "timed_out"):
        for h in list_pending(status):
            if interview_id in (h["graph_thread_id"], h["record_id"]):
                return h
    return None


def set_pending_status(graph_thread_id: str, status: str) -> None:
    conn().execute("UPDATE pending_handoffs SET status=? WHERE graph_thread_id=?", (status, graph_thread_id))


# ---------------------------------------------------------------- comments, cache, meta
def comment_processed(comment_id: str) -> bool:
    return conn().execute("SELECT 1 FROM processed_comments WHERE comment_id=?", (comment_id,)).fetchone() is not None


def mark_comment(comment_id: str, doc_id: str, outcome: str) -> None:
    conn().execute("INSERT OR REPLACE INTO processed_comments VALUES(?,?,?,?)", (comment_id, doc_id, outcome, _now()))


def cache_get(key: str) -> Any | None:
    row = conn().execute("SELECT data FROM research_cache WHERE key=?", (key,)).fetchone()
    return json.loads(row["data"]) if row else None


def cache_put(key: str, data: Any) -> None:
    conn().execute("INSERT OR REPLACE INTO research_cache VALUES(?,?,?)", (key, json.dumps(data, default=str), _now()))


def meta_get(key: str) -> str | None:
    row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(key: str, value: str) -> None:
    conn().execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
