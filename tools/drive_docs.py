"""create_or_update_prep_doc: writes the prep doc. Rewrites ONLY the agent's sections (1-6);
the trailing "My notes" section is never touched."""
from __future__ import annotations

import re

from langchain_core.tools import tool

from core.config import config, mock_google
from tools.common import TransientError, ok, safe_tool
from tools.mocks import drive_mock

MY_NOTES = "My notes"


def render_markdown(doc: dict) -> str:
    out = [f"# {doc['title']}", ""]
    for s in doc["agent_sections"]:
        out += [f"## {s['heading']}", "", s["body"].rstrip(), ""]
    out += [f"## {MY_NOTES}", "", doc.get("my_notes", "") or "_(Your space. The agent never edits this section.)_", ""]
    return "\n".join(out)


# ------------------------------------------------------------------ markdown -> Google Docs formatting
HEADINGS = {"# ": "HEADING_1", "## ": "HEADING_2", "### ": "HEADING_3", "#### ": "HEADING_4"}
_INLINE = re.compile(r"\*\*(?P<b>.+?)\*\*|`(?P<c>[^`]+)`|\[(?P<lt>[^\]]+)\]\((?P<lu>https?://[^)\s]+)\)"
                     r"|(?<![\w*])_(?P<i>[^\s_](?:[^\n]*?[^\s])?)_(?!\w)")  # inner underscores (Los_Angeles) are fine
GRAY = {"color": {"rgbColor": {"red": 0.42, "green": 0.42, "blue": 0.42}}}


def _u16(s: str) -> int:
    """Docs indexes count UTF-16 code units, not Python characters."""
    return len(s.encode("utf-16-le")) // 2


def _inline(text: str) -> tuple[str, list[tuple[int, int, str, str | None]]]:
    """Strip inline markdown; return plain text and (start, end, kind, url) spans in UTF-16 units."""
    out, spans, pos = "", [], 0
    for m in _INLINE.finditer(text):
        out += text[pos:m.start()]
        kind = next(k for k in ("b", "c", "lt", "i") if m.group(k) is not None)
        inner = m.group(kind)
        if kind == "b":  # bold may contain a link or italics; keep it simple and recurse once
            inner, sub = _inline(inner)
            spans += [(_u16(out) + a, _u16(out) + b, k, u) for a, b, k, u in sub]
        start = _u16(out)
        out += inner
        spans.append((start, _u16(out), kind, m.group("lu") if kind == "lt" else None))
        pos = m.end()
    return out + text[pos:], spans


def markdown_to_docs(md: str, start: int = 1) -> tuple[str, list[dict]]:
    """Convert the prep doc's markdown subset (#-####, '- ' bullets nested by 2 spaces, **bold**, _italic_,
    `code`, [text](url)) into plain text plus Docs API formatting requests for text inserted at `start`."""
    text, paras = "", []  # paras: (start, end, style, bullet_level|None, spans)
    for raw in md.splitlines():
        if not raw.strip():
            continue  # spacing comes from paragraph styles, not empty lines
        line, style, level = raw.rstrip(), "NORMAL_TEXT", None
        for prefix, st in HEADINGS.items():
            if line.startswith(prefix):
                line, style = line[len(prefix):], st
                break
        else:
            m = re.match(r"^( *)[-*] (.*)$", line)
            if m:
                level, line = len(m.group(1)) // 2, m.group(2)
        plain, spans = _inline(line)
        plain = "\t" * (level or 0) + plain  # leading tabs set the nesting; Docs removes them
        s = start + _u16(text)
        text += plain + "\n"
        shift = level or 0
        paras.append((s, start + _u16(text), style, level, [(s + shift + a, s + shift + b, k, u) for a, b, k, u in spans]))
    end = start + _u16(text)
    reqs: list[dict] = [
        {"updateTextStyle": {"range": {"startIndex": start, "endIndex": end}, "textStyle": {},
                             "fields": "bold,italic,link,weightedFontFamily,foregroundColor"}},
        {"deleteParagraphBullets": {"range": {"startIndex": start, "endIndex": end}}},
    ]
    for s, e, style, _, _ in paras:
        reqs.append({"updateParagraphStyle": {"range": {"startIndex": s, "endIndex": e},
                                              "paragraphStyle": {"namedStyleType": style}, "fields": "namedStyleType"}})
    for _, _, _, _, spans in paras:
        for a, b, kind, url in spans:
            style, fields = {"b": ({"bold": True}, "bold"),
                             "i": ({"italic": True, "foregroundColor": GRAY}, "italic,foregroundColor"),
                             "c": ({"weightedFontFamily": {"fontFamily": "Roboto Mono"}}, "weightedFontFamily"),
                             "lt": ({"link": {"url": url}}, "link")}[kind]
            reqs.append({"updateTextStyle": {"range": {"startIndex": a, "endIndex": b}, "textStyle": style, "fields": fields}})
    # Bullets last and from the bottom up: removing the nesting tabs shifts everything after each block.
    blocks: list[list[int]] = []
    for s, e, _, level, _ in paras:
        if level is None:
            continue
        if blocks and blocks[-1][1] == s:
            blocks[-1][1] = e
        else:
            blocks.append([s, e])
    for s, e in reversed(blocks):
        reqs.append({"createParagraphBullets": {"range": {"startIndex": s, "endIndex": e},
                                                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
    return text, reqs


# ------------------------------------------------------------------ real Google Docs
def _folder_id(drive) -> str:
    name = config()["drive"]["folder_name"]
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    found = drive.files().list(q=q, fields="files(id)").execute().get("files", [])
    if found:
        return found[0]["id"]
    return drive.files().create(body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
                                fields="id").execute()["id"]


def _my_notes_index(doc: dict) -> int | None:
    for el in doc["body"]["content"]:
        p = el.get("paragraph")
        if not p:
            continue
        text = "".join(r.get("textRun", {}).get("content", "") for r in p.get("elements", [])).strip()
        if text == MY_NOTES and p.get("paragraphStyle", {}).get("namedStyleType", "").startswith("HEADING"):
            return el["startIndex"]
    return None


def _real_write(doc_id: str | None, title: str, sections: list[tuple[str, str]]) -> dict:
    from tools.google_auth import is_transient, service

    try:
        drive, docs = service("drive", "v3"), service("docs", "v1")
        created = False
        if not doc_id:
            doc_id = drive.files().create(body={"name": title, "mimeType": "application/vnd.google-apps.document",
                                                "parents": [_folder_id(drive)]}, fields="id").execute()["id"]
            created = True
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
                {"insertText": {"location": {"index": 1}, "text": f"{MY_NOTES}\n\n"}},
                {"updateParagraphStyle": {"range": {"startIndex": 1, "endIndex": len(MY_NOTES) + 2},
                                          "paragraphStyle": {"namedStyleType": "HEADING_2"}, "fields": "namedStyleType"}},
            ]}).execute()
        else:
            drive.files().update(fileId=doc_id, body={"name": title}).execute()
        doc = docs.documents().get(documentId=doc_id).execute()
        notes_at = _my_notes_index(doc)
        if notes_at is None:  # user deleted the heading: append agent content at the end, never delete their text
            notes_at = doc["body"]["content"][-1]["endIndex"] - 1
        requests = []
        if notes_at > 1:
            requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": notes_at}}})
        md = "\n".join([f"# {title}"] + [f"## {h}\n{body.rstrip()}" for h, body in sections])
        text, fmt = markdown_to_docs(md, start=1)
        requests.append({"insertText": {"location": {"index": 1}, "text": text}})
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests + fmt}).execute()
        return {"doc_id": doc_id, "created": created, "url": f"https://docs.google.com/document/d/{doc_id}/edit"}
    except Exception as e:
        if is_transient(e):
            raise TransientError(str(e)) from e
        raise


def write_doc(doc_id: str | None, title: str, sections: list[tuple[str, str]]) -> dict:
    if mock_google():
        return drive_mock.create_or_update(doc_id, title, sections, render_markdown)
    return _real_write(doc_id, title, sections)


@tool
@safe_tool("Retry once; if Docs keeps failing, the local markdown copy in output/ is still written.")
def create_or_update_prep_doc(interview_id: str, content: dict) -> dict:
    """Create or update the prep doc for an interview in the 'Interview Prep' Drive folder.
    content = {"doc_id": str|None, "title": str, "sections": [{"heading": str, "body": str}, ...]}.
    Only the agent's sections are rewritten; 'My notes' is never modified."""
    sections = [(s["heading"], s["body"]) for s in content["sections"] if s["heading"] != MY_NOTES]
    res = write_doc(content.get("doc_id"), content["title"], sections)
    return ok(interview_id=interview_id, **res)
