"""research_company_process: 2-3 Tavily queries -> one structured-output call.
Retries once with a broadened query (drop level, then role). Cached by (company, role)."""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from core import db
from core.config import config
from core.llm import prompt, structured
from graph.schemas import CompanyProcess
from tools.common import TransientError, err, ok, privacy_safe_query, safe_tool, untrusted
from tools.web import tavily_search


def _queries(company: str, role: Optional[str], level: Optional[str]) -> list[list[str]]:
    full = [privacy_safe_query(company, level, role, "interview process rounds"),
            privacy_safe_query(company, role, "interview questions loop")]
    no_level = [privacy_safe_query(company, role, "interview process")]
    company_only = [privacy_safe_query(company, "software engineer interview process")]
    return [full, no_level, company_only]


def run_research(company: str, role: Optional[str] = None, level: Optional[str] = None) -> dict:
    key = f"process::{db.norm(company)}::{db.norm(role)}"
    cached = db.cache_get(key)
    if cached:
        return ok(**cached, cached=True)
    results, used_queries, errors = [], [], []
    for attempt, qs in enumerate(_queries(company, role, level)):
        for q in qs:
            try:
                res = tavily_search(q, max_results=5)
                used_queries.append(q)
                results.extend(res["results"])
            except TransientError as e:
                errors.append(str(e))
        if results or attempt >= 2:
            break  # retry once (or twice) with a broadened query only when nothing came back
    if not results:
        if errors:
            return err("search unavailable: " + errors[-1], "Search is down. Use what the emails/job post say, or mark round types unconfirmed.")
        return ok(rounds=[], notes="No public information found.", sources=[], confidence="low",
                  confidence_reason="no search results", queries=used_queries)
    seo = set(config()["research"]["seo_prep_domains"])
    seo_count = sum(1 for r in results if urlparse(r["url"]).netloc.replace("www.", "") in seo)
    snippets = "\n".join(f"- [{r['url']}] {r['title']}: {r['content'][:500]}" for r in results)
    proc = structured("company_process", CompanyProcess).invoke([
        SystemMessage(prompt("company_process")),
        HumanMessage(f"Company: {company}\nRole: {role}\nLevel: {level}\n"
                     f"Sources ({len(results)} results, {seo_count} from SEO prep sites):\n{untrusted(snippets, 5000)}"),
    ]).model_dump()
    # Deterministic guard: mostly SEO prep sites => confidence drops one notch.
    if results and seo_count / len(results) > 0.5 and proc["confidence"] != "low":
        proc["confidence"] = {"high": "medium", "medium": "low"}[proc["confidence"]]
        proc["confidence_reason"] += " (lowered: sources are mostly SEO prep sites)"
    proc["sources"] = proc["sources"] or [r["url"] for r in results]
    proc["queries"] = used_queries
    db.cache_put(key, proc)
    return ok(**proc)


@tool
@safe_tool("Research failed. Use what the emails and job post say; if round types are still unknown, hand off.")
def research_company_process(company: str, role: Optional[str] = None, level: Optional[str] = None) -> dict:
    """Research the company's typical interview loop for this role and level. Returns rounds, notes,
    sources, and confidence (high/medium/low). Only company, role and level are sent to web search.
    Use it when the emails don't state the round types."""
    return run_research(company, role, level)
