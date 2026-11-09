"""OAuth desktop flow (credentials.json -> token.json) with minimal scopes.

Scopes:
- gmail.readonly : read messages and threads
- gmail.labels   : create the "Interview Prep/*" labels
- gmail.modify   : ONLY used to add/remove labels on messages (no send tool exists)
- drive.file     : only files this app creates (prep docs) and their comments
"""
from __future__ import annotations

from functools import lru_cache

from core.config import ROOT

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
]
# Explicitly NOT requested: gmail.send, gmail.compose, mail.google.com, drive, documents (full).


@lru_cache(maxsize=1)
def credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = ROOT / "token.json"
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / "credentials.json"), SCOPES)
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json())
    return creds


@lru_cache(maxsize=None)
def service(name: str, version: str):
    from googleapiclient.discovery import build

    return build(name, version, credentials=credentials(), cache_discovery=False)


def is_transient(e: Exception) -> bool:
    status = getattr(getattr(e, "resp", None), "status", None)
    return status in (429, 500, 502, 503, 504) or "timed out" in str(e).lower()
