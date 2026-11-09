"""Fixture-backed web: Tavily search, page fetch, and link status (MOCK_WEB=true)."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from urllib.parse import urlparse

from core.config import ROOT

WEB = ROOT / "fixtures" / "web"


@lru_cache(maxsize=1)
def _pages() -> dict:
    return json.loads((WEB / "pages.json").read_text())


@lru_cache(maxsize=1)
def _search() -> list:
    return json.loads((WEB / "search.json").read_text())


@lru_cache(maxsize=1)
def _links() -> dict:
    return json.loads((WEB / "links.json").read_text())


def tavily_search(query: str, include_domains: list[str] | None = None, max_results: int = 5) -> dict:
    if os.getenv("MOCK_WEB_FAIL") == "search":
        from tools.common import TransientError

        raise TransientError("mock search outage")
    q = query.lower()
    results = []
    for entry in _search():
        if all(w in q for w in entry["match_all"]):
            for r in entry["results"]:
                if include_domains:
                    host = urlparse(r["url"]).netloc.lower()
                    if not any(host == d or host.endswith("." + d) for d in include_domains):
                        continue
                results.append(r)
    return {"query": query, "results": results[:max_results]}


def fetch(url: str) -> tuple[int, str]:
    page = _pages().get(url)
    if page is None:
        return 404, "Not Found"
    return page["status"], page["text"]


def link_status(url: str) -> str:
    if url in _links()["broken"]:
        return "broken"
    page = _pages().get(url)
    if page is not None:
        return "ok" if page["status"] < 400 else "broken"
    host = urlparse(url).netloc.lower()
    if host in _links()["ok_domains"]:
        return "ok"
    return "unverified"
