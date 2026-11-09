"""`cli.py doctor`: read-only preflight for real runs. Makes one tiny model call and one
search; on first use it opens the Google consent screen. It never sends, labels or writes."""
from __future__ import annotations

import os

from core.config import ROOT, mock_google, mock_llm, mock_web


def _line(ok: bool | None, name: str, detail: str) -> bool:
    mark = {True: "✓", False: "✗", None: "–"}[ok]
    print(f"  {mark} {name}: {detail}")
    return ok is not False


def run_doctor() -> int:
    print(f"Mode: MOCK_GOOGLE={mock_google()}  MOCK_WEB={mock_web()}  MOCK_LLM={mock_llm()}\n")
    good = True
    # model
    if mock_llm():
        good &= _line(None, "model", "offline policy model (set MOCK_LLM=false for a real model)")
    else:
        try:
            from core.llm import _real_model

            r = _real_model().invoke("Reply with the single word OK.")
            good &= _line(True, "model", f"{os.getenv('MODEL_PROVIDER', 'openai')}:{os.getenv('MODEL_NAME', 'gpt-6.1-terra')} answered {str(r.content)[:20]!r}")
        except Exception as e:
            good &= _line(False, "model", f"{type(e).__name__}: {str(e)[:200]} (check MODEL_PROVIDER, MODEL_NAME, API key, provider package)")
    # web search
    if mock_web():
        _line(None, "web search", "mock fixtures (set MOCK_WEB=false and TAVILY_API_KEY for real search)")
    elif not os.getenv("TAVILY_API_KEY"):
        good &= _line(False, "web search", "TAVILY_API_KEY is not set")
    else:
        try:
            from tools.web import tavily_search

            n = len(tavily_search("software engineer interview process", max_results=2)["results"])
            good &= _line(True, "web search", f"Tavily returned {n} result(s)")
        except Exception as e:
            good &= _line(False, "web search", f"{type(e).__name__}: {str(e)[:200]}")
    # Google
    if mock_google():
        _line(None, "google", "mock fixtures (set MOCK_GOOGLE=false for your Gmail/Drive)")
    elif not (ROOT / "credentials.json").exists():
        good &= _line(False, "google", f"missing {ROOT / 'credentials.json'} (OAuth Desktop client from Google Cloud Console)")
    else:
        try:
            from tools.google_auth import SCOPES, credentials, service

            creds = credentials()  # opens the consent screen the first time
            missing = [s for s in SCOPES if s not in (creds.scopes or SCOPES)]
            email = service("gmail", "v1").users().getProfile(userId="me").execute()["emailAddress"]
            labels = service("gmail", "v1").users().labels().list(userId="me").execute().get("labels", [])
            ours = [l["name"] for l in labels if l["name"].startswith("Interview Prep")]
            good &= _line(not missing, "gmail", f"signed in as {email}; Interview Prep labels: {ours or 'none yet (created on first label)'}"
                          + (f"; MISSING SCOPES {missing} - delete token.json and re-consent" if missing else ""))
            service("drive", "v3").files().list(pageSize=1, fields="files(id)").execute()
            service("docs", "v1")
            good &= _line(True, "drive/docs", "APIs reachable (drive.file: only files this app creates)")
        except Exception as e:
            good &= _line(False, "google", f"{type(e).__name__}: {str(e)[:250]}")
    # tracing
    if os.getenv("LANGSMITH_TRACING", "").lower() == "true":
        _line(bool(os.getenv("LANGSMITH_API_KEY")), "langsmith", f"project {os.getenv('LANGSMITH_PROJECT', 'default')}"
              + ("" if os.getenv("LANGSMITH_API_KEY") else " - LANGSMITH_API_KEY not set"))
    else:
        _line(None, "langsmith", "tracing off (LANGSMITH_TRACING=true to see tool-call paths)")
    print("\nReady for a real run." if good else "\nFix the ✗ items above, then run doctor again.")
    return 0 if good else 1
