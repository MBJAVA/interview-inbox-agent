"""Small shared pieces for the agent graphs: retry policy, budget accounting, context blocks."""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import RetryPolicy

from tools.common import TransientError


def _is_transient(e: Exception) -> bool:
    if isinstance(e, TransientError):
        return True
    name = type(e).__name__.lower()
    return any(k in name for k in ("timeout", "ratelimit", "apiconnection", "serviceunavailable", "internalserver"))


# For nodes that call external APIs (LLM providers, Gmail, Drive, web).
RETRY = RetryPolicy(max_attempts=3, initial_interval=1.0, backoff_factor=2.0, retry_on=_is_transient)


def reset_messages() -> list:
    return [RemoveMessage(id=REMOVE_ALL_MESSAGES)]


def context_block(data: dict[str, Any]) -> str:
    return "<context>\n" + json.dumps(data, indent=1, default=str) + "\n</context>"


def enforce_tool_budget(state: dict, ai: AIMessage, agent: str) -> tuple[AIMessage, dict]:
    """Count tool calls against the per-run budget. If the model asks for more than is left, the
    calls are dropped, the stop is recorded in build_log/budget_exhausted, and the loop ends."""
    calls = list(ai.tool_calls or [])
    used = state.get("tool_call_count", 0) or 0
    budget = state.get("tool_call_budget", 40) or 40
    tokens = (getattr(ai, "usage_metadata", None) or {}).get("total_tokens", 0) or 0
    update: dict = {"token_count": (state.get("token_count") or 0) + tokens,
                    "step_count": (state.get("step_count") or 0) + 1}
    from core.config import budget as cfg_budget

    # Scales with the tool-call budget (raised for big loops), so the two limits stay in proportion.
    token_budget = int(cfg_budget("per_email_tokens") * max(1.0, budget / cfg_budget("per_email_tool_calls")))
    over_tokens = update["token_count"] > token_budget
    if calls and (used + len(calls) > budget or over_tokens):
        note = (f"{agent}: token budget hit ({update['token_count']}/{token_budget})" if over_tokens else
                f"{agent}: tool-call budget hit ({used}/{budget})") + f"; stopped before {len(calls)} more call(s)"
        ai = AIMessage(content=(ai.content or "") + f"\n[budget] {note}", id=ai.id)
        update["budget_exhausted"] = list(state.get("budget_exhausted") or []) + [note]
        update["build_log"] = [{"tool": f"{agent}.budget", "outcome": note, "fallback": "stopped and reported"}]
        calls = []
    update["tool_call_count"] = used + len(calls)
    update["tool_path"] = [f"{agent}:{c['name']}#{c['id'][-8:]}" for c in calls]
    return ai, update


def clean_convo(messages: list) -> list:
    """Messages for a follow-up structured call: drop system messages and any tool calls that never
    got a result (e.g. dropped by an iteration budget), so provider APIs accept the history."""
    from langchain_core.messages import SystemMessage

    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    out = []
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        if isinstance(m, AIMessage) and m.tool_calls and not all(c["id"] in answered for c in m.tool_calls):
            m = AIMessage(content=m.content or "(stopped before running more tools)", id=m.id)
        out.append(m)
    return out


def tool_results_since_last_ai(messages: list) -> list[ToolMessage]:
    out = []
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            out.append(m)
        elif isinstance(m, AIMessage):
            break
    return list(reversed(out))


def log_tool_failures(messages: list, agent: str) -> list[dict]:
    """Turn {"ok": false} tool results into build_log entries ("which tools failed")."""
    entries = []
    for m in tool_results_since_last_ai(messages):
        try:
            data = json.loads(m.content) if isinstance(m.content, str) else {}
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get("ok") is False:
            entries.append({"tool": f"{agent}:{m.name}", "outcome": f"failed: {str(data.get('error'))[:140]}",
                            "fallback": f"model decided next step (hint: {str(data.get('hint'))[:120]})"})
    return entries
