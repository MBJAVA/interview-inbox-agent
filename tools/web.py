"""Web backend: Tavily search, page fetch, link check. Mock or real (MOCK_WEB)."""
from __future__ import annotations

import re

import httpx

from core.config import mock_web
from tools.common import TransientError, html_to_text
from tools.mocks import web_mock

UA = {"User-Agent": "interview-prep-agent/1.0 (link checker)"}


def tavily_search(query: str, include_domains: list[str] | None = None, max_results: int = 5,
                  time_range: str | None = None) -> dict:
    if mock_web():
        return web_mock.tavily_search(query, include_domains, max_results)
    from langchain_tavily import TavilySearch

    kwargs = {"max_results": max_results, "topic": "general"}
    if include_domains:
        kwargs["include_domains"] = include_domains
    if time_range:
        kwargs["time_range"] = time_range
    try:
        res = TavilySearch(**kwargs).invoke({"query": query})
    except Exception as e:  # network / quota
        raise TransientError(str(e)) from e
    if isinstance(res, str):
        return {"query": query, "results": []}
    return {"query": query, "results": [
        {"url": r.get("url"), "title": r.get("title", ""), "content": (r.get("content") or "")[:800]}
        for r in res.get("results", [])]}


def fetch_text(url: str) -> tuple[int, str]:
    if mock_web():
        return web_mock.fetch(url)
    try:
        r = httpx.get(url, headers=UA, follow_redirects=True, timeout=15)
    except httpx.TimeoutException as e:
        raise TransientError(f"timeout fetching {url}") from e
    text = html_to_text(r.text) if "html" in r.headers.get("content-type", "") else r.text
    return r.status_code, text[:12000]


_LEETCODE = re.compile(r"^https?://(?:www\.)?leetcode\.com/problems/([a-z0-9-]+)/?(?:[?#].*)?$")


def _leetcode_status(slug: str) -> str:
    """LeetCode answers 403 to any non-browser request, so ask its public problem lookup instead."""
    try:
        r = httpx.post("https://leetcode.com/graphql", timeout=10,
                       headers={**UA, "Referer": "https://leetcode.com", "Content-Type": "application/json"},
                       json={"query": "query q($s: String!) { question(titleSlug: $s) { questionId } }",
                             "variables": {"s": slug}})
        if r.status_code != 200:
            return "unverified"
        data = (r.json().get("data") or {})
        return "ok" if (data.get("question") or {}).get("questionId") else "broken"
    except (httpx.HTTPError, ValueError):
        return "unverified"


def link_status(url: str) -> str:
    if mock_web():
        return web_mock.link_status(url)
    m = _LEETCODE.match(url or "")
    if m:
        return _leetcode_status(m.group(1))
    try:
        with httpx.Client(headers=UA, follow_redirects=True, timeout=10) as c:
            r = c.head(url)
            if r.status_code in (403, 405, 429) or r.status_code >= 500:
                r = c.get(url)
            if r.status_code < 400:
                return "ok"
            if r.status_code in (404, 410):
                return "broken"
            return "unverified"  # 401/403/429/5xx: can't tell
    except (httpx.TimeoutException, httpx.ConnectError):
        return "unverified"
