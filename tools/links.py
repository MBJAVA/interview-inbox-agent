"""check_link: httpx HEAD -> GET fallback. Returns ok / broken / unverified per URL.

It returns a Command so the results land in state.checked_links, which accept_questions
uses to make sure every accepted link was actually checked.
"""
from __future__ import annotations

import json
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from tools.web import link_status


@tool
def check_link(urls: list[str], tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Check one or more URLs (HEAD, falling back to GET). Each result is 'ok', 'broken' (404/410), or
    'unverified' (blocked, timeout, or can't tell). For a broken link decide: find a replacement, swap
    in another question, or keep the question with link_status 'unverified' and no link."""
    results = {}
    for u in urls[:15]:
        try:
            results[u] = link_status(u)
        except Exception as e:  # tool failures are data
            results[u] = "unverified"
            results[u + "#error"] = str(e)[:120]
    content = json.dumps({"ok": True, "results": {k: v for k, v in results.items() if not k.endswith("#error")}})
    return Command(update={
        "checked_links": {k: v for k, v in results.items() if not k.endswith("#error")},
        "messages": [ToolMessage(content, tool_call_id=tool_call_id, name="check_link")],
    })
