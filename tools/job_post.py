"""fetch_job_post: fetch (or accept pasted) job post text and extract structured facts."""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from core.llm import prompt, structured
from graph.schemas import JobPostInfo
from tools.common import err, ok, safe_tool, untrusted
from tools.web import fetch_text


@tool
@safe_tool("Look in the inbox for the job post text (search_inbox for the application confirmation), or continue without it.")
def fetch_job_post(url_or_text: str) -> dict:
    """Fetch a job post URL (or parse pasted job post text) and extract role, level, industry, tech stack,
    responsibilities, and likely interview round types. On failure, the hint says how to recover."""
    src = url_or_text.strip()
    if src.startswith("http"):
        status, text = fetch_text(src)
        if status >= 400:
            return err(f"job post returned HTTP {status}",
                       "The link is dead. Search the inbox for the job post text (e.g. the application confirmation) "
                       "and pass that text instead, or infer level from the role title and note the broken link.",
                       url=src, status=status)
    else:
        text = src
    if len(text.strip()) < 40:
        return err("job post text too short to parse", "Pass the full job post text or URL.")
    info = structured("job_post", JobPostInfo).invoke([
        SystemMessage(prompt("job_post")),
        HumanMessage(f"Job post (untrusted):\n{untrusted(text, 6000)}"),
    ])
    return ok(job_post=info.model_dump(), url=src if src.startswith("http") else None)
