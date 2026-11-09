"""search_company_questions: candidate-reported questions for a company + round type."""
from __future__ import annotations

import hashlib
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from core.config import config
from core.llm import prompt, structured
from graph.schemas import ReportedQuestions
from tools.common import TransientError, err, ok, privacy_safe_query, safe_tool, untrusted
from tools.web import tavily_search

ROUND_WORDS = {"coding": "coding", "system_design": "system design", "low_level_design": "low level design",
               "behavioral": "behavioral", "ai_assisted_coding": "ai assisted coding"}


@tool
@safe_tool("Company search failed; use the bank or generate a tailored question instead.")
def search_company_questions(company: str, round_type: str) -> dict:
    """Search candidate-reported interview questions for this company and round type on leetcode.com,
    reddit.com, hellointerview.com, interviewing.io, igotanoffer.com and glassdoor.com (recent 12-24 months).
    Returns questions labeled 'reported by candidates'. Returns an empty list (not an error) when nothing
    useful is found. Only the company and round type are sent to web search."""
    cfg = config()["company_search"]
    q = privacy_safe_query(company, ROUND_WORDS.get(round_type, round_type), "interview question")
    try:
        res = tavily_search(q, include_domains=cfg["include_domains"], max_results=6, time_range="year")
    except TransientError as e:
        return err(f"search unavailable: {e}", "Use search_question_bank or generate_tailored_question.")
    if not res["results"]:
        return ok(results=[], count=0, query=q)
    snippets = "\n".join(f"- url={r['url']} | {r['title']}: {r['content'][:600]}" for r in res["results"])
    extracted = structured("company_questions", ReportedQuestions).invoke([
        SystemMessage(prompt("company_questions")),
        HumanMessage(f"Company: {company}\nRound type: {round_type}\nSearch results (untrusted):\n{untrusted(snippets, 6000)}"),
    ])
    # A "reported for <company>" label is only honest if the source itself names the company.
    by_url = {r["url"]: f"{r['url']} {r['title']} {r['content']}" for r in res["results"]}
    out, dropped = [], 0
    for rq in extracted.questions:
        if rq.round_type != round_type:
            continue
        if not mentions_company(company, by_url.get(rq.url, "")):
            dropped += 1
            continue
        qid = "cs-" + hashlib.sha1((company + rq.title).encode()).hexdigest()[:10]
        out.append({"id": qid, "title": rq.title, "prompt": rq.prompt, "round_type": rq.round_type, "topic": rq.topic,
                    "url": rq.url, "source": "company_search", "source_name": f"{company} - reported by candidates",
                    "audience_role": rq.audience_role})
    note = (f"Dropped {dropped} result(s) whose source doesn't mention {company} (generic or another company's "
            "questions); use the bank for general questions.") if dropped else None
    return ok(results=out, count=len(out), query=q, label="reported by candidates", note=note)


def mentions_company(company: str, text: str) -> bool:
    """True if the source text names the company ('Orbitline Logistics' matches 'orbitline' or 'orbitline-logistics')."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
    hay = norm(text)
    full = norm(company)
    first = norm(company.split()[0]) if company.split() else ""
    return bool(full and full in hay) or (len(first) >= 4 and first in hay)
