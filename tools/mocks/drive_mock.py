"""Mock Google Drive/Docs + comments. Docs are JSON files in output/mock_drive/ with
the agent-owned sections stored separately from the user's "My notes" section, plus a
rendered .md for humans to read."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from core import clock
from core.config import ROOT, path

COMMENT_FIXTURES = ROOT / "fixtures" / "comments"


def _dir() -> Path:
    d = path("mock_drive")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _doc_file(doc_id: str) -> Path:
    return _dir() / f"{doc_id}.json"


def list_docs() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(_dir().glob("doc_*.json"))]


def get_doc(doc_id: str) -> dict | None:
    f = _doc_file(doc_id)
    return json.loads(f.read_text()) if f.exists() else None


def create_or_update(doc_id: str | None, title: str, sections: list[tuple[str, str]], render) -> dict:
    created = False
    doc = get_doc(doc_id) if doc_id else None
    if doc is None:
        doc_id = doc_id or f"doc_{uuid.uuid4().hex[:10]}"
        doc = {"doc_id": doc_id, "title": title, "folder": "Interview Prep", "created_at": clock.now().isoformat(),
               "my_notes": "", "agent_sections": [], "update_count": 0}
        created = True
    # Only the agent-owned sections are replaced. doc["my_notes"] is never written here.
    doc["title"] = title
    doc["agent_sections"] = [{"heading": h, "body": b} for h, b in sections]
    doc["updated_at"] = clock.now().isoformat()
    doc["update_count"] = doc.get("update_count", 0) + (0 if created else 1)
    _doc_file(doc_id).write_text(json.dumps(doc, indent=1))
    (_dir() / f"{doc_id}.md").write_text(render(doc))
    return {"doc_id": doc_id, "created": created, "url": f"mock://drive/{doc_id}"}


# ------------------------------------------------------------------ comments
def _comment_state_file() -> Path:
    return _dir() / "comments_state.json"


def _comment_state() -> dict:
    f = _comment_state_file()
    return json.loads(f.read_text()) if f.exists() else {"replies": {}, "resolved": []}


def list_comments(doc_id: str) -> list[dict]:
    doc = get_doc(doc_id)
    if not doc:
        raise KeyError(f"doc {doc_id} not found (only app-created docs are visible)")
    state = _comment_state()
    out = []
    for f in sorted(COMMENT_FIXTURES.glob("*.json")):
        data = json.loads(f.read_text())
        if data["company"].lower() not in doc["title"].lower():
            continue
        for c in data["comments"]:
            c = dict(c)
            c["resolved"] = c.get("resolved") or c["id"] in state["resolved"]
            c["replies"] = state["replies"].get(c["id"], [])
            out.append(c)
    return out


def reply(doc_id: str, comment_id: str, text: str, resolve: bool) -> dict:
    if not get_doc(doc_id):
        raise KeyError(f"doc {doc_id} not found")
    state = _comment_state()
    state["replies"].setdefault(comment_id, []).append({"text": text, "at": clock.now().isoformat()})
    if resolve and comment_id not in state["resolved"]:
        state["resolved"].append(comment_id)
    _comment_state_file().write_text(json.dumps(state, indent=1))
    return {"comment_id": comment_id, "resolved": resolve}
