"""Graph state. One schema shared by the parent graph and every agent subgraph.

Each agent subgraph starts with an *init* node that clears `messages` (RemoveMessage
REMOVE_ALL_MESSAGES) so every agent gets its own scratch conversation, while the
structured fields (interview, questions, build_log, ...) carry across agents and,
via the SqliteSaver checkpointer and the SQLite record, across days.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, Optional

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

RoundType = Literal["coding", "ai_assisted_coding", "system_design", "low_level_design", "behavioral"]
ROUND_TYPES: list[str] = ["coding", "ai_assisted_coding", "system_design", "low_level_design", "behavioral"]
DIFFICULTIES: list[str] = ["easy", "medium", "hard"]
LEVELS: list[str] = ["junior", "mid", "senior", "staff", "principal"]
CLASS_LABELS: list[str] = [
    "interview_invite", "scheduling_request", "interview_confirmation", "reschedule",
    "next_round", "recruiter_outreach", "rejection_or_offer", "not_relevant",
]


class EmailData(TypedDict, total=False):
    message_id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    body: str
    calendar_invite: Optional[dict]


class Classification(TypedDict, total=False):
    label: str
    confidence: Literal["high", "medium", "low"]
    reason: str
    reschedule_scope: Optional[Literal["date_only", "round_changed"]]


class Interview(TypedDict, total=False):
    company: Optional[str]
    role: Optional[str]
    level: Optional[str]
    round_types: list[str]
    interview_datetime: Optional[str]
    format: Optional[str]
    job_post: Optional[str]
    job_post_summary: Optional[str]
    recruiter_name: Optional[str]
    industry: Optional[str]
    focus_topics: list[str]
    notes: Optional[str]
    status: Literal["active", "waiting", "needs_input", "closed"]
    doc_id: Optional[str]
    doc_url: Optional[str]
    count_overrides: dict  # {"system_design:hard": 5}


class Question(TypedDict, total=False):
    id: str
    title: str
    prompt: str
    round_type: str
    topic: str
    difficulty: str
    difficulty_reason: str
    source: Literal["bank", "company_search", "generated"]
    source_name: str
    url: Optional[str]
    link_status: Literal["ok", "broken", "unverified", "n/a"]
    competency: Optional[str]


class Slot(TypedDict):
    round: str
    difficulty: str
    filled: int
    target: int


class Feedback(TypedDict, total=False):
    text: str
    source: Literal["cli", "comment"]
    comment_id: Optional[str]
    doc_id: Optional[str]
    classification: Optional[str]
    scope: Optional[dict]
    result_line: Optional[str]


class PendingHandoff(TypedDict, total=False):
    question: str
    trigger: str
    found: dict
    created_at: str
    timeout_at: str


# ------------------------------------------------------------------ reducers
def questions_reducer(current: list | None, update: Any) -> list:
    """`questions` accepts either a full list (replace) or {"add": [...], "remove": [ids]}.

    The dict form lets parallel tool calls (several accept/remove calls in one model
    turn) be applied safely without overwriting each other.
    """
    current = list(current or [])
    if update is None:
        return current
    if isinstance(update, list):
        return update
    remove = set(update.get("remove", []))
    out = [q for q in current if q["id"] not in remove]
    seen = {q["id"] for q in out}
    for q in update.get("add", []):
        if q["id"] not in seen:
            out.append(q)
            seen.add(q["id"])
    return out


def dict_merge(current: dict | None, update: dict | None) -> dict:
    if update is None:
        return dict(current or {})
    if update.get("__replace__"):
        return {k: v for k, v in update.items() if k != "__replace__"}
    out = dict(current or {})
    out.update(update)
    return out


def log_reducer(current: list | None, update: Any) -> list:
    """Append-only log with idempotent merge.

    A subgraph added as a node returns its *full* state to the parent, so the parent sees
    the whole list again. Every entry gets a unique `_id` the first time it is appended,
    and entries whose `_id` is already present are skipped, so re-applying is a no-op.
    """
    import uuid

    if update is None:
        return list(current or [])
    if isinstance(update, dict) and update.get("__replace__") is not None:
        base: list = []
        update = update["__replace__"]
    else:
        base = list(current or [])
    seen = {_entry_id(e) for e in base}
    for e in update:
        if isinstance(e, dict) and "_id" not in e:
            e = {**e, "_id": uuid.uuid4().hex[:12]}
        elif isinstance(e, str) and "#" not in e:
            e = f"{e}#{uuid.uuid4().hex[:8]}"
        k = _entry_id(e)
        if k not in seen:
            base.append(e)
            seen.add(k)
    return base


def _entry_id(e: Any) -> str:
    return e.get("_id", repr(e)) if isinstance(e, dict) else str(e)


class AgentState(TypedDict, total=False):
    # conversation scratch for the currently running agent
    messages: Annotated[list[AnyMessage], add_messages]

    # entry / routing
    mode: Literal["email", "feedback", "rebuild", "correction"]
    email_message_id: str
    next_step: str  # set by each agent's final node; read by parent routers

    # identity
    interview_id: str          # canonical record id
    provisional_id: str        # gt_<gmail thread id>; == LangGraph thread_id for email runs
    gmail_thread_ids: list[str]
    existing_record: Optional[dict]

    email: EmailData
    classification: Classification
    interview: Interview
    field_sources: Annotated[dict, dict_merge]
    guessed_fields: list[str]
    missing_fields: list[str]
    conflicts: list[str]
    open_questions: list[str]
    pending_handoff: Optional[PendingHandoff]
    handoff_answer: Optional[str]
    company_process: Optional[dict]

    # curation
    questions: Annotated[list[Question], questions_reducer]
    unfilled_slots: list[Slot]
    slot_targets: dict          # {"coding": {"hard": [2,3], ...}}
    coverage_gaps: list[str]
    refine_scope: Optional[dict]  # {"rounds": [...], "difficulties": [...], "question_ids": [...], "directive": str}
    checked_links: Annotated[dict, dict_merge]
    difficulty_labels: Annotated[dict, dict_merge]  # stable labels by question id
    skip_curation: bool
    questions_before: list       # snapshot at curator start (for "what changed")
    last_question_count: int     # progress check between coverage iterations
    last_question_ids: Optional[list]  # same check by question ids (a swap keeps the count)
    research_confidence: Optional[dict]

    # feedback
    feedback: Feedback

    # loop control and budgets
    iteration_count: int
    max_iterations: int
    step_count: int
    tool_call_count: int
    tool_call_budget: int
    token_count: int
    budget_exhausted: list[str]
    last_error: Optional[str]

    # reporting
    build_log: Annotated[list[dict], log_reducer]
    tool_path: Annotated[list[str], log_reducer]
    revision_history: list[dict]
    revision_note: Optional[str]
    labels_to_apply: list[str]
    outcome: str
