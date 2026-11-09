"""Offline scripted policy model (MOCK_LLM=true).

A deterministic stand-in for the real chat model so the full graph, demo, evals and tests
run with no API key. It implements the same interface the graphs use (`bind_tools`,
`with_structured_output`, `invoke`) and makes its decisions only from what a real model
would see: the context block, the conversation, and tool results. It is NOT the product's
intelligence and its eval numbers are not a substitute for a real-model run; it exists so
control flow, state, persistence, interrupts, budgets and tool-failure handling can be
exercised and tested offline.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

# ====================================================================== helpers
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
TZ = {"PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "ET": "America/New_York",
      "EST": "America/New_York", "EDT": "America/New_York", "CT": "America/Chicago", "MT": "America/Denver", "UTC": "UTC"}
AGENCY = ("recruit", "staffing", "talent", "agency", "search", "hire")
VENDORS = ("karat", "hackerrank", "codesignal", "coderpad", "codility", "brighthire")  # interview vendors, not employers
GENERIC_TLDS = {"com", "io", "dev", "co", "net", "org", "app"}
STOP_PHRASES = {"Hi", "Hello", "Thanks", "Best", "Great", "Job", "Your", "We", "The", "Interview", "Senior", "Staff",
                "Software", "Engineer", "Calendar", "Recruiting", "Zoom", "Thursday", "Friday", "Monday", "Tuesday",
                "Wednesday", "November", "October", "Unfortunately"}

ROUND_PATTERNS = [
    ("low_level_design", r"low[- ]level design|object[- ]oriented design|\blld\b|\bood\b"),
    ("system_design", r"system design"),
    ("ai_assisted_coding", r"ai[- ]assisted|with copilot|ai coding"),
    ("coding", r"\bcoding\b|live coding|technical (?:phone )?screen|algorithm|\bkarat\b|hackerrank|codesignal|codility|coderpad interview"),
    ("behavioral", r"behavioral|values interview|hiring manager"),
]
ROLE_RE = re.compile(
    r"\b(?:(?:Senior|Staff|Principal|Junior|Lead)\s+)?(?:Software|Backend|Frontend|Platform|Data)\s+(?:Software\s+)?Engineer"
    r"(?:\s+II{1,2}\b)?(?:,\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)?(?:\s+\((?!Mid|Senior|Junior|Staff)[A-Z][^)]*\))?")
DT_RE = re.compile(
    r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:,\s*(\d{4}))?,?\s*(?:at\s+)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(pt|pst|pdt|et|est|edt|ct|mt|utc)?", re.I)


def _id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def _context(messages: list[BaseMessage]) -> dict:
    for m in messages:
        if isinstance(m, HumanMessage) and isinstance(m.content, str) and "<context>" in m.content:
            raw = m.content.split("<context>", 1)[1].split("</context>", 1)[0]
            try:
                return json.loads(raw)
            except Exception:
                return {}
    return {}


def _strip_untrusted(s: str) -> str:
    return re.sub(r"</?untrusted_content>", "", s or "").strip()


def _calls(messages: list[BaseMessage]) -> list[tuple[str, dict, dict]]:
    """(tool name, args, parsed result) for every tool call that has a result."""
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    out = []
    for m in messages:
        if isinstance(m, AIMessage):
            for c in m.tool_calls or []:
                r = results.get(c["id"])
                if r is None:
                    continue
                try:
                    data = json.loads(r.content) if isinstance(r.content, str) else {}
                except Exception:
                    data = {"ok": False, "error": str(r.content)[:200]}
                out.append((c["name"], c["args"], data))
    return out


def _called(calls, name: str, **match) -> bool:
    return any(n == name and all(a.get(k) == v for k, v in match.items()) for n, a, _ in calls)


def _ai(content: str = "", calls: list[tuple[str, dict]] | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=[{"name": n, "args": a, "id": _id(), "type": "tool_call"} for n, a in (calls or [])])


def _domain(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender or "")
    if not m:
        return ""
    parts = m.group(1).lower().split(".")
    return ".".join(parts[-2:])  # recruiting subdomains (talent.x.com) belong to the company


def is_agency(domain: str) -> bool:
    return any(h in domain for h in AGENCY + VENDORS)


def find_company(text: str, domain: str) -> str | None:
    m = re.search(r"[Mm]y client,?\s+([A-Z][\w&]+(?:\s+[A-Z][\w&]+){0,2})", text)
    if m:
        return m.group(1)
    if any(v in (domain or "") for v in VENDORS):
        m = re.search(r"(?:interview|assessment|screen) (?:with|for) ([A-Z][\w&]+(?: [A-Z][\w&]+){0,2})", text)
        return m.group(1) if m and m.group(1).lower() not in VENDORS else None
    if not domain or is_agency(domain):
        return None
    parts = domain.split(".")
    name, tld = parts[-2], parts[-1]
    stems = ([name + tld] if tld not in GENERIC_TLDS else []) + [name]
    phrases = re.findall(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}", text)
    for stem in stems:
        for ph in phrases:
            words = ph.split()
            for k in range(1, len(words) + 1):
                cand = " ".join(words[:k])
                key = cand.lower().replace(" ", "")
                if len(key) >= 4 and (key == stem or (key.startswith(stem) and len(key) - len(stem) <= 4) or
                                      (stem.startswith(key) and len(stem) - len(key) <= 3 and k == len(words))):
                    if cand.split()[0] not in STOP_PHRASES:
                        return cand
    return None


def company_from_domain(domain: str) -> str | None:
    if not domain or is_agency(domain) or domain.split(".")[0] in ("gmail", "outlook", "yahoo", "hotmail", "icloud"):
        return None
    return domain.split(".")[0].capitalize()


def find_role(text: str) -> str | None:
    m = ROLE_RE.search(text)
    return m.group(0).strip() if m else None


def level_from(text: str) -> str | None:
    t = text.lower()
    for word, lvl in [("principal", "principal"), ("staff", "staff"), ("senior", "senior"), ("sr.", "senior"),
                      ("junior", "junior"), ("mid-level", "mid"), ("mid level", "mid"), (" ii", "mid")]:
        if word in t:
            return lvl
    return None


def rounds_from(text: str) -> list[str]:
    t = text.lower()
    t = re.split(r"followed later|later on,|after that,", t)[0]
    t = re.sub(r"instead of [\w\s-]+?(?=[,.;]|$)", " ", t)
    t = re.sub(r"\bno (?:[\w-]+ )?(?:system design|low[- ]level design|behavioral|coding)\b", " ", t)
    found = []
    for rnd, pat in ROUND_PATTERNS:
        if re.search(pat, t):
            found.append(rnd)
    if "low_level_design" in found and "system_design" in found and not re.search(r"system design", re.sub(r"low[- ]level design|object[- ]oriented design", "", t)):
        found.remove("system_design")
    return found


def datetime_from(text: str) -> str | None:
    m = DT_RE.search(text)
    if not m:
        return None
    month, day, year, hour, minute, ampm, tz = m.groups()
    h = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    zone = ZoneInfo(TZ.get((tz or "PT").upper(), "America/Los_Angeles"))
    dt = datetime(int(year or 2026), MONTHS.index(month.lower()) + 1, int(day), h, int(minute or 0), tzinfo=zone)
    return dt.isoformat()


TOPIC_WORDS = [("hash map", "hash maps"), ("hashmap", "hash maps"), ("string", "strings"), ("array", "arrays"),
               ("graph", "graphs/BFS-DFS"), ("bfs", "graphs/BFS-DFS"), ("sorting", "sorting"), ("tree", "trees"),
               ("dynamic programming", "dynamic programming"), ("heap", "heaps"), ("complexity", "complexity analysis")]


def topics_from(text: str) -> list[str]:
    t = text.lower()
    return list(dict.fromkeys(label for word, label in TOPIC_WORDS if word in t))


def format_from(text: str) -> str | None:
    t = text.lower()
    if "onsite" in t or "office" in t:
        return "onsite"
    if any(w in t for w in ("zoom", "video", "google meet", "virtual", "meet.")):
        return "video"
    if "phone" in t:
        return "phone"
    return None


def industry_from(text: str) -> str | None:
    t = text.lower()
    for kw, ind in [("payment", "payments/fintech"), ("fintech", "fintech"), ("trading", "trading/fintech"),
                    ("robotics", "robotics"), ("health", "healthcare"), ("analytics", "analytics"), ("retail", "retail")]:
        if kw in t:
            return ind
    return None


# ====================================================================== triage
def classify_text(subject: str, body: str) -> dict:
    b, s = body.lower(), subject.lower()
    if "unsubscribe" in b or "newsletter" in b or "ignore all previous instructions" in b:
        return {"label": "not_relevant", "confidence": "high", "reason": "bulk/marketing mail (newsletter or promo), not the candidate's own interview"}
    if any(k in b for k in ("unfortunately", "not to move forward", "not move forward", "move forward with other", "pleased to offer", "offer letter")):
        return {"label": "rejection_or_offer", "confidence": "high", "reason": "the company declined to move forward (or made an offer)"}
    if "reschedul" in b or "move your interview" in b:
        scope = "round_changed" if re.search(r"will now be|instead of|switch(?:ing)? to", b) else "date_only"
        return {"label": "reschedule", "confidence": "high", "reason": f"the interview time is being moved ({scope.replace('_', ' ')})", "reschedule_scope": scope}
    if any(k in b for k in ("next round", "next stage", "move you forward")):
        return {"label": "next_round", "confidence": "high", "reason": "candidate advanced to a later round"}
    if any(k in b for k in ("availability", "share a few times", "when are you available")) or \
            re.search(r"suggest (?:a few|some) (?:days|times)|days and times|times that work", b):
        return {"label": "scheduling_request", "confidence": "high", "reason": "asks for availability; time not set yet"}
    if re.search(r"next step will be|if you move forward", b) and rounds_from(body) and not DT_RE.search(body):
        return {"label": "scheduling_request", "confidence": "medium", "reason": "describes the next round; no time scheduled yet"}
    if "confirm" in s or "this confirms" in b:
        return {"label": "interview_confirmation", "confidence": "high", "reason": "confirms a scheduled interview"}
    if ("interview" in b or "onsite" in b) and (DT_RE.search(body) or rounds_from(body) or "invite" in b):
        return {"label": "interview_invite", "confidence": "high", "reason": "invites the candidate to a specific interview"}
    if any(k in b for k in ("came across your profile", "opportunit", "open to a")):
        return {"label": "recruiter_outreach", "confidence": "high", "reason": "cold outreach, no interview arranged"}
    if "interview" in b:
        return {"label": "not_relevant", "confidence": "medium", "reason": "mentions interviews but isn't about the candidate's process"}
    return {"label": "not_relevant", "confidence": "low", "reason": "can't tell what this email is about"}


def triage_policy(messages):
    ctx = _context(messages)
    calls = _calls(messages)
    e = ctx.get("email", {})
    body = _strip_untrusted(e.get("body", ""))
    vague = len(body) < 400 or e.get("subject", "").lower().startswith("re:")
    if ctx.get("thread_message_count", 1) > 1 and vague and not _called(calls, "get_thread"):
        return _ai("The message is a reply; reading the thread first.", [("get_thread", {"thread_id": e["thread_id"]})])
    return _ai("Ready to classify.")


def triage_structured(schema, messages):
    ctx = _context(messages)
    e = ctx.get("email", {})
    return schema(**classify_text(e.get("subject", ""), _strip_untrusted(e.get("body", ""))))


# ====================================================================== extraction
def _gather(messages):
    """Collect texts with their source labels, newest/most relevant first."""
    ctx = _context(messages)
    e = ctx.get("email", {})
    current = (_strip_untrusted(e.get("body", "")) + "\n" + e.get("subject", ""))
    texts = [("email body", current)]
    for name, args, data in _calls(messages):
        if name == "get_thread" and data.get("ok"):
            for idx, m in enumerate(data["messages"], 1):
                # only messages that had arrived by the time of this email
                if m["message_id"] != e.get("message_id") and m.get("date", "") <= e.get("date", "9"):
                    texts.append((f"thread message {idx}", m["subject"] + "\n" + _strip_untrusted(m["body"])))
        if name == "get_message" and data.get("ok"):
            m = data["message"]
            if m["message_id"] != e.get("message_id"):
                texts.append((f"earlier email '{m['subject']}'", m["subject"] + "\n" + _strip_untrusted(m["body"])))
    answers = [m.content for m in messages if isinstance(m, HumanMessage) and isinstance(m.content, str) and "user answered" in m.content.lower()]
    return ctx, e, texts, answers


def _extract_fields(messages) -> dict:
    ctx, e, texts, answers = _gather(messages)
    domain = _domain(e.get("from", ""))
    out: dict[str, Any] = {"sources": {}}
    for src, t in texts:
        for key, fn in (("company", lambda x: find_company(x, domain)), ("role", find_role), ("interview_datetime", datetime_from),
                        ("format", format_from)):
            if key not in out:
                v = fn(t)
                if v:
                    out[key], out["sources"][key] = v, src
        if "job_post" not in out:
            m = re.search(r"https?://\S*(?:job|career)\S*", t)
            if m:
                out["job_post"], out["sources"]["job_post"] = m.group(0).rstrip(".,)"), src
    # agency domain without a named client: also check earlier emails' domains are not used
    rounds_by_src = [(src, rounds_from(t)) for src, t in texts]
    cur_rounds = rounds_by_src[0][1]
    older = [(s, r) for s, r in rounds_by_src[1:] if r]
    label = (ctx.get("classification") or {}).get("label")
    conflicts = []
    if cur_rounds:
        out["round_types"], out["sources"]["round_types"] = cur_rounds, "email body"
        if older and label not in ("reschedule", "next_round") and not set(cur_rounds) & set(older[0][1]):
            conflicts.append(f"The latest email says {', '.join(cur_rounds)}, but {older[0][0]} says {', '.join(older[0][1])} for the same interview.")
    elif older:
        out["round_types"], out["sources"]["round_types"] = older[0][1], older[0][0]
    jp = [d for n, a, d in _calls(messages) if n == "fetch_job_post" and d.get("ok")]
    if jp:
        j = jp[-1]["job_post"]
        out["job_post_summary"] = j.get("summary")
        if j.get("industry"):
            out["industry"], out["sources"]["industry"] = j["industry"], "job post"
        if j.get("level"):
            out["level"], out["sources"]["level"] = j["level"], "job post"
    if "level" not in out and out.get("role"):
        lv = level_from(out["role"])
        if lv:
            out["level"], out["sources"]["level"] = lv, "inferred from role title"
    if "level" not in out:
        for src, t in texts:
            lv = level_from(t) if "mid-level" in t.lower() or "mid level" in t.lower() else None
            if lv:
                out["level"], out["sources"]["level"] = lv, src
                break
    if "industry" not in out:
        ind = industry_from(" ".join(t for _, t in texts))
        if ind:
            out["industry"], out["sources"]["industry"] = ind, "email body"
    for n, a, d in _calls(messages):
        if n == "read_attachment" and d.get("ok"):
            guide = _strip_untrusted(d["text"])
            tps = topics_from(guide)
            if tps:
                out["focus_topics"], out["sources"]["focus_topics"] = tps, f"attachment: {a['filename']}"
            fm = re.search(r"(\d+) minutes?: (?:two or three|\d+(?: or \d+)?) coding problems[^.]*", guide, re.I)
            out["guide_note"] = (f"Prep guide ({a['filename']}): " + (fm.group(0) if fm else "read") +
                                 ("; any mainstream language" if "any mainstream language" in guide.lower() else ""))
    if "company" not in out and company_from_domain(domain):
        out["company"], out["sources"]["company"] = company_from_domain(domain), f"sender domain ({_domain(e.get('from', ''))})"
    alltext = " ".join(t for _, t in texts).lower()
    if "coderpad" in alltext:
        out["notes"] = "uses CoderPad"
    m = re.search(r"(\d+)-minute (karat|hackerrank|codesignal|codility)", alltext)
    if m:
        out["notes"] = f"{m.group(2).capitalize()} interview ({m.group(1)} min, third-party vendor)"
    if out.get("guide_note"):
        out["notes"] = "; ".join(x for x in (out.get("notes"), out["guide_note"]) if x)
    name = re.match(r"\s*([^<]+?)\s*<", e.get("from", ""))
    if name and not any(w in name.group(1).lower() for w in ("calendar", "team", "careers", "scheduling", "newsletter")):
        out["recruiter_name"], out["sources"]["recruiter_name"] = name.group(1), "sender"
    for ans in answers:
        a = _strip_untrusted(ans.split("):", 1)[-1] if "):" in ans else ans.split(":", 1)[-1]).split("\n")[0]
        m = re.search(r"(?i:company is|it's with|it is with|client is|it's|with)\s+([A-Z][\w&]+(?: [A-Z][\w&]+){0,2})", a)
        if m:
            out["company"], out["sources"]["company"] = m.group(1), "user answer"
        r = rounds_from(a)
        if r:
            out["round_types"], out["sources"]["round_types"] = r, "user answer"
        conflicts = []
    out["conflicts"] = conflicts
    out["agency"] = is_agency(domain)
    out["domain"] = domain
    out["label"] = label
    return out


def extraction_policy(messages):
    ctx = _context(messages)
    calls = _calls(messages)
    e = ctx.get("email", {})
    f = _extract_fields(messages)
    domain = f["domain"]
    if ctx.get("thread_message_count", 1) > 1 and not _called(calls, "get_thread"):
        return _ai("Reading the thread for earlier details.", [("get_thread", {"thread_id": e["thread_id"]})])
    # look for missing company/role in inbox history (application confirmations etc.)
    if (not f.get("company") or not f.get("role")) and domain and not _called(calls, "search_inbox", query=f"from:{domain}"):
        return _ai("Company or role missing; searching earlier emails from this sender's domain.",
                   [("search_inbox", {"query": f"from:{domain}", "max_results": 10})])
    for n, a, d in calls:
        if n == "search_inbox" and d.get("ok"):
            todo = [r["id"] for r in d["results"] if r["id"] != e.get("message_id") and not _called(calls, "get_message", message_id=r["id"])]
            if todo and (not f.get("role") or not f.get("job_post")):
                return _ai("Reading earlier emails that may hold the role or job post.", [("get_message", {"message_id": i}) for i in todo[:2]])
    unread = [a["filename"] for a in (e.get("attachments") or [])
              if (a.get("mime_type") in ("application/pdf", "text/plain") or a["filename"].lower().endswith((".pdf", ".txt")))
              and not _called(calls, "read_attachment", filename=a["filename"])]
    if unread:
        return _ai("An attachment may describe the interview; reading it.",
                   [("read_attachment", {"message_id": e["message_id"], "filename": fn}) for fn in unread[:2]])
    if f.get("job_post") and not _called(calls, "fetch_job_post"):
        return _ai("Fetching the job post to infer level, stack and industry.", [("fetch_job_post", {"url_or_text": f["job_post"]})])
    jp_fail = [d for n, a, d in calls if n == "fetch_job_post" and not d.get("ok")]
    if jp_fail and domain and not _called(calls, "search_inbox", query=f'from:{domain} "job"'):
        return _ai("Job post link failed; looking for the job post text in the inbox.",
                   [("search_inbox", {"query": f'from:{domain} "job"', "max_results": 5})])
    if not f.get("round_types") and f.get("company") and not _called(calls, "research_company_process"):
        return _ai("Round types not stated; researching the company's typical loop.",
                   [("research_company_process", {"company": f["company"], "role": f.get("role"), "level": f.get("level")})])
    return _ai("Done gathering.")


def extraction_structured(schema, messages):
    f = _extract_fields(messages)
    calls = _calls(messages)
    s = f["sources"]

    def fv(k):
        return {"value": f.get(k), "source": s.get(k)} if f.get(k) else {"value": None, "source": None}

    rounds = f.get("round_types") or []
    stated = bool(rounds)
    if not rounds:
        proc = [d for n, a, d in calls if n == "research_company_process" and d.get("ok")]
        if proc and proc[-1].get("rounds"):
            rounds, stated = proc[-1]["rounds"], False
    decision, question, reason = "complete", None, "required fields found"
    if f["conflicts"]:
        decision, question = "handoff", f["conflicts"][0] + " Which round type is it?"
        reason = "sources conflict"
    elif not f.get("company"):
        decision = "handoff"
        question = (f"The recruiter at {f['domain']} doesn't name the hiring company (I checked the email, thread and earlier emails "
                    "from that domain). Which company is this interview with?")
        reason = "hiring company not identified"
    elif f.get("label") == "scheduling_request" and not f.get("interview_datetime"):
        decision, reason = "wait", "scheduling request: the time will arrive in a later email"
    return schema(**{
        "company": fv("company"), "role": fv("role"), "round_types": rounds, "round_types_source": s.get("round_types"),
        "round_types_stated": stated, "level": fv("level"), "interview_datetime": fv("interview_datetime"),
        "format": fv("format"), "job_post": fv("job_post"), "job_post_summary": f.get("job_post_summary"),
        "recruiter_name": fv("recruiter_name"), "industry": fv("industry"), "notes": f.get("notes"),
        "focus_topics": f.get("focus_topics") or [], "focus_topics_source": s.get("focus_topics"),
        "conflicts": f["conflicts"], "decision": decision, "decision_reason": reason, "handoff_question": question,
    })


# ====================================================================== curator
GEN_TEMPLATES = {
    "coding": [
        ("Reconcile payment events", "Input: two lists of payment events (id, amount_cents, status). Output: ids missing from either list and ids whose amount or status differ. Constraints: up to 10^6 events each; O(n) time.", "hashing/reconciliation"),
        ("Merge overlapping holds", "Input: a list of authorization holds as [start, end, amount]. Output: total held amount over time as merged intervals. Constraints: up to 10^5 holds; O(n log n).", "intervals/sweep line"),
        ("Detect duplicate charges in a stream", "Input: a stream of (card_id, amount, timestamp). Output: the charges that repeat the same card and amount within 60 seconds. Constraints: up to 10^6 events; O(1) amortized per event.", "sliding window/hashing"),
        ("Shortest fee path between currencies", "Input: currency pairs with conversion fees. Output: the cheapest conversion path from A to B. Constraints: up to 10^4 pairs; Dijkstra-level complexity.", "graphs/shortest path"),
        ("Top merchants by refund rate", "Input: a list of transactions (merchant, type). Output: the k merchants with the highest refund rate (min 100 transactions). Constraints: up to 10^6 rows; O(n log k).", "heap/aggregation"),
    ],
    "ai_assisted_coding": [
        ("Extend a webhook consumer with an AI assistant", "Using an AI coding assistant, add idempotent retry handling to an existing webhook consumer in a small service. Review the assistant's diff, run the provided tests, write one failing test for an edge case it missed, then correct the code. Done = all tests pass and you can explain every change.", "idempotency/verification"),
        ("Add pagination with an AI pair", "Using an AI coding assistant, add cursor-based pagination to an existing REST endpoint. Inspect the generated code for off-by-one and ordering bugs, run the integration tests, verify behavior with a large dataset, and fix what the assistant got wrong.", "API change/verification"),
        ("Refactor a legacy module with an AI assistant", "Use an AI coding assistant to split a 600-line module into testable units without changing behavior. Run the existing test suite before and after, review each suggested change, reject unsafe ones, and correct any regressions you find.", "refactoring/verification"),
        ("Build a CLI feature with an AI assistant", "Using an AI coding assistant, build a small CLI command that imports a CSV into the app's database. Review the generated parsing and error handling, run it against malformed input, verify the results in the database, and fix the issues you find.", "feature build/verification"),
        ("Debug a flaky test with an AI assistant", "Use an AI coding assistant to investigate a flaky concurrency test. Evaluate its hypotheses, run the test repeatedly to verify, inspect the proposed fix for hidden race conditions, and correct it before merging.", "debugging/verification"),
        ("Add metrics with an AI assistant", "Using an AI coding assistant, instrument a service with latency and error-rate metrics. Review the generated code for cardinality problems, run the service locally to verify the metrics, and correct anything that would break dashboards.", "observability/verification"),
    ],
    "system_design": [
        ("Design an idempotent payouts API", "Design an API that pays out sellers exactly once despite client retries, partial failures and bank timeouts. Cover idempotency keys, state machine, reconciliation and auditing.", "payments/idempotency"),
        ("Design a card authorization service", "Design a service that approves card transactions within a 100ms budget, with fraud checks, holds and captures, and graceful degradation when dependencies fail.", "payments/low latency"),
        ("Design a multi-tenant job scheduler", "Design a scheduler that runs millions of delayed and recurring jobs for many tenants with fairness, retries and exactly-once side effects where needed.", "scheduling/multi-tenancy"),
        ("Design a feature flag service", "Design a service for evaluating feature flags in under 5ms for thousands of services, with targeting rules, gradual rollouts and safe propagation of changes.", "configuration/low latency"),
        ("Design a fraud signal pipeline", "Design a streaming system that computes per-card fraud features in near real time and serves them to the authorization path.", "streaming/fraud"),
    ],
    "low_level_design": [
        ("Design a pluggable rate-limited job scheduler", "Design the classes for an in-process job scheduler with priorities, per-tenant rate limits, cancellation and retry policies. Show the extension points and thread-safety strategy.", "concurrency/scheduling"),
        ("Design an in-memory order book", "Design the classes for an order book supporting limit and market orders with price-time priority, cancels and trade events.", "order matching/fintech"),
        ("Design a write-ahead log library", "Design a write-ahead log library with segment files, checksums, fsync policies, crash recovery and compaction hooks. Show the class structure and failure handling.", "storage/durability"),
        ("Design a thread-safe connection pool", "Design a generic connection pool with max size, idle eviction, health checks and fair waiting under contention. Show interfaces and locking.", "concurrency/resource pooling"),
        ("Design a pluggable retry and circuit-breaker library", "Design classes for retry policies (backoff, jitter) and a circuit breaker with half-open probing, usable around any client call.", "resilience/patterns"),
    ],
    "behavioral": [
        ("Owning a payments incident", "Tell me about an incident that affected customers' money or data. What did you own, how did you communicate, and what changed afterward?", "ownership"),
        ("Navigating an ambiguous mandate", "Describe a time you were given a goal with no clear plan. How did you decide what to do first and how did you know it was working?", "ambiguity"),
        ("Disagreeing on a design review", "Tell me about a design review where you strongly disagreed. How did you handle it and what was the outcome?", "conflict"),
        ("Influencing a roadmap", "Describe how you got another team to prioritize work your project depended on.", "influence"),
        ("A launch that failed", "Tell me about a launch that didn't go as planned. What did you learn and change?", "failure"),
    ],
}


def _curator_view(messages):
    ctx = _context(messages)
    calls = _calls(messages)
    targets = ctx.get("slot_targets", {})
    current = {q["id"]: q for q in ctx.get("current_questions", [])}
    accepted_ids, rejected, removed = set(), {}, set()
    submitted = {}
    for n, a, d in calls:
        if n == "accept_questions":
            for c in a.get("candidates", []):
                submitted[c["id"]] = c
            accepted_ids |= set(d.get("accepted", []))
            for r in d.get("rejected", []):
                rejected[r["id"]] = r["reason"]
        if n == "remove_questions":
            removed |= set(d.get("removed", []))
    have = [q for i, q in current.items() if i not in removed]
    for i in accepted_ids:
        if i in submitted and i not in removed:
            have.append(submitted[i])
    checked = {}
    for n, a, d in calls:
        if n == "check_link":
            checked.update(d.get("results", {}))
    return ctx, calls, targets, have, rejected, removed, checked


def _pool(calls, rnd):
    pool = []
    for n, a, d in calls:
        if not d.get("ok"):
            continue
        if n == "search_question_bank" and a.get("round_type") == rnd:
            pool += [dict(q) for q in d.get("results", [])]
        if n == "search_company_questions" and a.get("round_type") == rnd:
            pool += [dict(q, difficulty=None) for q in d.get("results", [])]
        if n == "generate_tailored_question" and a.get("round_type") == rnd:
            pool.append(dict(d["question"]))
    seen, out = set(), []
    for q in pool:
        if q["id"] not in seen:
            seen.add(q["id"])
            out.append(q)
    return out


def _keyword(directive: str) -> str | None:
    d = (directive or "").lower()
    for pat in (r"relevant to\s+([a-z-]+)", r"focus on\s+([a-z-]+)", r"about\s+([a-z-]+)", r"more\s+([a-z-]+)"):
        m = re.search(pat, d)
        if m and m.group(1) not in ("relevant", "questions", "the", "a"):
            return m.group(1)
    return None


def curator_policy(messages):
    ctx, calls, targets, have, rejected, removed, checked = _curator_view(messages)
    interview = ctx.get("interview", {})
    level = interview.get("level") or "senior"
    industry = interview.get("industry")
    company = interview.get("company")
    ref = ctx.get("refinement") or {}
    scope = ref.get("scope") or {}
    directive = (scope.get("directive") or "").lower()
    kw = _keyword(directive)

    # 1) refinement: decide what to swap out (once)
    if scope and not _called(calls, "remove_questions"):
        rounds = scope.get("rounds") or list(targets)
        ids = []
        for q in have:
            if q["round_type"] not in rounds:
                continue
            if scope.get("question_ids") and q["id"] in scope["question_ids"]:
                ids.append(q["id"])
            elif ("harder" in directive or "too easy" in directive) and q["difficulty"] == "easy":
                ids.append(q["id"])
            elif ("easier" in directive or "too hard" in directive) and q["difficulty"] == "hard":
                ids.append(q["id"])
            elif kw and kw not in (q.get("title", "") + " " + q.get("topic", "")).lower() and len(ids) < 2:
                ids.append(q["id"])
        if ids:
            return _ai("Swapping out questions that don't fit the feedback.", [("remove_questions", {"ids": ids, "reason": directive or "refinement"})])

    used = {q["id"] for q in have} | set(rejected) | removed
    selected, searches, gens = [], [], []
    order = ctx.get("fill_order") or ["hard", "medium", "easy"]
    gen_counts: dict = {}
    for n, a, d in calls:
        if n == "generate_tailored_question":
            gen_counts[(a["round_type"], a["difficulty"])] = gen_counts.get((a["round_type"], a["difficulty"]), 0) + 1
    rounds_in_scope = (scope.get("rounds") or list(targets)) if scope else list(targets)
    for rnd in targets:
        if rnd not in rounds_in_scope:
            continue
        pool = [q for q in _pool(calls, rnd) if q["id"] not in used]
        focus = [w[:5] for t in (interview.get("focus_topics") or []) for w in re.findall(r"[a-z]{4,}", t.lower())]
        if focus and not kw:
            pool.sort(key=lambda q: 0 if any(f in (q.get("title", "") + " " + q.get("topic", "")).lower() for f in focus) else 1)
        elif kw:
            pool.sort(key=lambda q: 0 if kw in (q.get("title", "") + q.get("topic", "") + " ".join(q.get("industry_tags", []))).lower() else 1)
        elif industry:
            ik = industry.split("/")[0][:6]
            pool.sort(key=lambda q: 0 if ik in (" ".join(q.get("industry_tags", [])) + q.get("topic", "")).lower() else 1)
        for diff in order:
            lo, hi = targets[rnd].get(diff, [0, 0])
            filled = sum(1 for q in have if q["round_type"] == rnd and q["difficulty"] == diff) + \
                sum(1 for q in selected if q["round_type"] == rnd and q["difficulty"] == diff)
            need = hi - filled
            if need <= 0:
                continue
            cands = [q for q in pool if (q.get("difficulty") in (diff, None)) and q["id"] not in {s["id"] for s in selected}]
            ok_cands = [q for q in cands if not q.get("url") or checked.get(q["url"]) != "broken"]
            take = ok_cands[:need]
            if len(take) < need:  # last resort: keep a broken-link question without its link
                take += [dict(q, keep_unverified=True) for q in cands if q not in ok_cands][: need - len(take)]
            for q in take:
                selected.append({"id": q["id"], "round_type": rnd, "difficulty": diff, "title": q["title"], "prompt": q["prompt"],
                                 "topic": q.get("topic", ""), "source": q.get("source", "bank"), "source_name": q.get("source_name", ""),
                                 "url": q.get("url"), "competency": q.get("competency") or (q.get("topic") if rnd == "behavioral" else None),
                                 "audience_role": q.get("audience_role"), "keep_unverified": q.get("keep_unverified", False)})
            need -= len(take)
            if need <= 0:
                continue
            if rnd == "ai_assisted_coding" or (_called(calls, "search_question_bank", round_type=rnd, difficulty=diff)
                                               and (not company or _called(calls, "search_company_questions", round_type=rnd))):
                k0 = gen_counts.get((rnd, diff), 0)
                if k0 < 6:
                    for k in range(min(need, 6 - k0)):
                        gens.append(("generate_tailored_question", {"round_type": rnd, "level": level, "difficulty": diff,
                                                                    "industry": industry, "topic": f"{kw or industry or 'general'} #{k0 + k}",
                                                                    "job_summary": interview.get("job_post_summary")}))
                    gen_counts[(rnd, diff)] = k0 + need
            elif not _called(calls, "search_question_bank", round_type=rnd, difficulty=diff):
                searches.append(("search_question_bank", {"round_type": rnd, "level": level, "difficulty": diff,
                                                          "exclude_ids": sorted(used), "limit": 6}))
            elif company and not _called(calls, "search_company_questions", round_type=rnd) and \
                    ("search_company_questions", {"company": company, "round_type": rnd}) not in searches:
                searches.append(("search_company_questions", {"company": company, "round_type": rnd}))
    unchecked = sorted({q["url"] for q in selected if q.get("url") and q["url"] not in checked})
    if unchecked:
        return _ai("Checking links before accepting.", [("check_link", {"urls": unchecked})] + searches + gens)
    if selected:
        return _ai("Proposing questions.", [("accept_questions", {"candidates": selected})] + searches + gens)
    if searches or gens:
        return _ai("Finding more candidates.", searches + gens)
    return _ai("All slots handled as far as the sources allow.")


def coverage_structured(schema, messages):
    ctx = _context(messages)
    status = ctx.get("slot_status", [])
    gaps = [{"round": s["round"], "difficulty": s["difficulty"], "filled": s["filled"], "target": s["min"]}
            for s in status if s["filled"] < s["min"]]
    issues = ctx.get("rule_violations_found_by_validator", [])
    return schema(meets_goal=not gaps and not issues, gaps=gaps, quality_issues=issues,
                  summary=f"{len(gaps)} slot(s) below target")


def calibration_structured(schema, messages):
    data = json.loads(messages[-1].content)
    return schema(difficulty=data["requested_difficulty"],
                  reason=f"{data['requested_difficulty']} for a {data['target_level']} candidate: {data.get('topic') or data['title']}")


def generator_structured(schema, messages):
    p = dict(re.findall(r"(\w+)=(.*)", messages[-1].content))
    rnd, diff = p["round_type"], p["difficulty"]
    m = re.search(r"#(\d+)", p.get("topic", ""))
    k = int(m.group(1)) if m else 0
    base = {"easy": 0, "medium": 1, "hard": 2}[diff]
    t = GEN_TEMPLATES[rnd]
    title, prompt, topic = t[(base * 2 + k) % len(t)]
    return schema(title=title, prompt=prompt, topic=topic, competency=topic if rnd == "behavioral" else None)


# ====================================================================== web helpers
def job_post_structured(schema, messages):
    text = _strip_untrusted(messages[-1].content.split(":", 1)[-1])
    first = text.strip().splitlines()[0] if text.strip() else ""
    role = first.split(" - ", 1)[1].strip() if " - " in first else find_role(text)
    lv = None
    m = re.search(r"Level:\s*(\w+)", text)
    if m:
        lv = level_from(m.group(1))
    lv = lv or level_from(role or "")
    stack = [w for w in ("Go", "Python", "Java", "Kotlin", "Scala", "Postgres", "Kafka", "Spark", "Iceberg") if re.search(rf"\b{w}\b", text)]
    sentences = re.split(r"(?<=[.])\s+", text.replace("\n", " "))
    return schema(role=role, level=lv, industry=industry_from(text), stack=stack, responsibilities=[],
                  likely_round_types=rounds_from(text), summary=" ".join(sentences[1:3])[:400])


def company_process_structured(schema, messages):
    text = messages[-1].content
    urls = re.findall(r"\[(https?://[^\]]+)\]", text)
    rounds = rounds_from(text)
    seo = sum(1 for u in urls if any(d in u for d in ("igotanoffer", "interviewquery", "prepfully", "designgurus")))
    non_seo = len(urls) - seo
    conf = "medium" if non_seo >= 2 else "low"
    return schema(rounds=rounds, notes=f"Reported rounds: {', '.join(rounds) or 'unclear'}", sources=urls, confidence=conf,
                  confidence_reason=f"{conf.capitalize()}: based on {len(urls)} source(s), {seo} SEO prep site(s), no official source")


def company_questions_structured(schema, messages):
    text = messages[-1].content
    rnd = re.search(r"Round type:\s*(\w+)", text).group(1)
    out = []
    for url, title, content in re.findall(r"- url=(\S+) \| ([^:]+?(?::[^:]+?)?): (.+)", text):
        t = title.split(": ", 1)[-1] if ": " in title else title
        if not re.search(r"design|implement|given|asked|build|tell me|describe", content.lower()) or "guide" in title.lower():
            continue  # not a concrete question
        aud = "product_manager" if re.search(r"product manager|\bpm\b|product-manager", (title + url + content).lower()) else "software_engineer"
        out.append({"title": t.strip(), "prompt": content.strip()[:300], "round_type": rnd, "topic": t.strip().lower()[:40],
                    "url": url, "audience_role": aud})
    return schema(questions=out)


# ====================================================================== feedback
ROUND_WORDS = [("system_design", r"system design|\bsd\b"), ("low_level_design", r"low[- ]level|\blld\b|object[- ]oriented"),
               ("ai_assisted_coding", r"ai[- ]assisted"), ("behavioral", r"behavioral"), ("coding", r"\bcoding\b")]


def _fb_rounds(text: str) -> list[str]:
    t = text.lower()
    out = []
    for r, pat in ROUND_WORDS:
        if re.search(pat, t):
            out.append(r)
            t = re.sub(pat, " ", t)
    return out


def _fb_match_question(ctx: dict, text: str) -> dict | None:
    t = (text + " " + ctx.get("quoted_text", "")).lower()
    best, score = None, 0
    for q in ctx.get("questions", []):
        words = {w for w in re.findall(r"[a-z]{4,}", q["title"].lower())}
        s = sum(1 for w in words if w in t)
        if s > score:
            best, score = q, s
    return best if score >= 2 else None


def feedback_policy(messages):
    ctx = _context(messages)
    calls = _calls(messages)
    text = _strip_untrusted(ctx.get("feedback", ""))
    if re.search(r"\bwhy\b|\?", text.lower()):
        q = _fb_match_question(ctx, _strip_untrusted(text) + " " + _strip_untrusted(ctx.get("quoted_text", "")))
        if q and not _called(calls, "get_question_details"):
            return _ai("Looking up the question.", [("get_question_details", {"question_id": q["id"]})])
    return _ai("Ready.")


def feedback_structured(schema, messages):
    ctx = _context(messages)
    calls = _calls(messages)
    text = _strip_untrusted(ctx.get("feedback", ""))
    t = text.lower()
    rounds = _fb_rounds(t)
    m = re.search(r"(\d+)\s+(easy|medium|hard)\s+([a-z -]+?)\s+questions", t)
    if m:
        rnd = (_fb_rounds(m.group(3)) or rounds or ["coding"])[0]
        return schema(classification="count_change", scope={"level": "round", "rounds": [rnd], "difficulties": [m.group(2)]},
                      directive=f"set {rnd} {m.group(2)} to {m.group(1)}",
                      count_override={"round": rnd, "difficulty": m.group(2), "filled": 0, "target": int(m.group(1))})
    if any(k in t for k in ("practiced", "went well", "thanks", "thank you", "noted", "fyi")) and "?" not in t:
        return schema(classification="note")
    if re.search(r"\bwhy\b|\?", t):
        det = [d for n, a, d in calls if n == "get_question_details" and d.get("ok")]
        if det:
            q = det[-1]["question"]
            ans = (f"'{q['title']}' is labeled {q['difficulty']} because: {q.get('difficulty_reason')}. "
                   f"Topic: {q.get('topic')}; source: {q.get('source_name')} ({q.get('source')}).")
            return schema(classification="question", scope={"level": "question", "question_ids": [q["id"]], "rounds": [q["round_type"]]}, answer=ans)
        return schema(classification="unclear", clarifying_question="Which question are you asking about? Quote its title and I'll explain.")
    if any(k in t for k in ("too easy", "harder", "more challenging")):
        return schema(classification="revision_request", scope={"level": "round" if rounds else "doc", "rounds": rounds},
                      directive="harder: swap out easy questions")
    if any(k in t for k in ("too hard", "easier")):
        return schema(classification="revision_request", scope={"level": "round" if rounds else "doc", "rounds": rounds},
                      directive="easier: swap out hard questions")
    m = re.search(r"(?:not relevant to|more relevant to|focus on|more about)\s+([a-z-]+)", t)
    if m or "replace" in t:
        q = _fb_match_question(ctx, text)
        ids = [q["id"]] if q and "replace" in t else []
        return schema(classification="revision_request",
                      scope={"level": "question" if ids else ("round" if rounds else "doc"), "rounds": rounds, "question_ids": ids},
                      directive=f"more relevant to {m.group(1)}" if m else "replace with a better-fitting question")
    return schema(classification="unclear", clarifying_question="What would you like me to change: a specific question, one round, or the whole list?")


# ====================================================================== baseline
BASELINE_MEMORY = {
    "coding": [("Two Sum", "https://leetcode.com/problems/two-sum/"), ("LRU Cache", "https://leetcode.com/problems/lru-cache/"),
               ("Explain time vs. space complexity", "https://www.bigocheatsheet.com/")],
    "system_design": [("Design Twitter", "https://www.educative.io/courses/grokking-the-system-design-interview/twitter"),
                      ("Design a rate limiter", "https://bytebytego.com/courses/system-design-interview/design-a-rate-limiter")],
    "low_level_design": [("Design a parking lot", "https://www.geeksforgeeks.org/design-parking-lot-system/"),
                         ("Design an LRU cache", "https://leetcode.com/problems/lru-cache/")],
    "behavioral": [("Tell me about yourself", None), ("Why this company?", None)],
    "ai_assisted_coding": [("Design a chat app", None)],
}


def baseline_structured(schema, messages):
    text = messages[-1].content
    subj = re.search(r"Subject: (.*)", text).group(1)
    sender = re.search(r"From: (.*)", text).group(1)
    body = text.split("Body:", 1)[-1]
    cls = classify_text(subj, body)
    dom = _domain(sender)
    company = None
    if dom:
        name = dom.split(".")[-2]
        company = name.capitalize()  # one-shot guess from the sender domain (wrong for agencies)
    role = find_role(subj + "\n" + body)
    rounds = rounds_from(body)
    qs = []
    for r in rounds:
        for title, url in BASELINE_MEMORY.get(r, []):
            qs.append({"round_type": r, "difficulty": "medium", "title": title, "url": url})
    return schema(label=cls["label"], company=company, role=role, level=level_from(role or ""), round_types=rounds,
                  interview_datetime=datetime_from(body), questions=qs)


# ====================================================================== model class
POLICIES = {"triage": triage_policy, "extraction": extraction_policy, "curator": curator_policy, "feedback": feedback_policy}
STRUCTURED = {"triage_classify": triage_structured, "extraction_result": extraction_structured, "coverage": coverage_structured,
              "calibration": calibration_structured, "generator": generator_structured, "job_post": job_post_structured,
              "company_process": company_process_structured, "company_questions": company_questions_structured,
              "feedback_classify": feedback_structured, "baseline": baseline_structured}


def _as_messages(x) -> list[BaseMessage]:
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        return [HumanMessage(x)]
    if hasattr(x, "to_messages"):
        return x.to_messages()
    return [HumanMessage(str(x))]


class FakePolicyModel(BaseChatModel):
    purpose: str
    tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "offline-policy"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tool_names": [getattr(t, "name", str(t)) for t in tools]})

    def with_structured_output(self, schema, **kwargs):
        fn = STRUCTURED[self.purpose]
        return RunnableLambda(lambda x: fn(schema, _as_messages(x)), name=f"offline_{self.purpose}")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        ai = POLICIES[self.purpose](messages)
        bad = [c["name"] for c in ai.tool_calls if c["name"] not in self.tool_names]
        if bad:  # a policy may never call a tool that isn't bound
            ai = AIMessage(content=f"(skipped unbound tools {bad})")
        ai.id = ai.id or "run-" + hashlib.sha1(str(len(messages)).encode()).hexdigest()[:8] + uuid.uuid4().hex[:6]
        return ChatResult(generations=[ChatGeneration(message=ai)])
