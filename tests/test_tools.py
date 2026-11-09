from graph.state import log_reducer, questions_reducer
from tools.common import privacy_safe_query
from tools.ics import parse_ics


def test_tool_failure_is_data_not_exception(sandbox):
    from tools.job_post import fetch_job_post

    r = fetch_job_post.invoke({"url_or_text": "https://marlowehealth.com/careers/senior-software-engineer-old"})
    assert r["ok"] is False and "hint" in r and "inbox" in r["hint"]


def test_research_search_outage_returns_error(sandbox, monkeypatch):
    from tools.company_research import research_company_process

    monkeypatch.setenv("MOCK_WEB_FAIL", "search")
    r = research_company_process.invoke({"company": "Acme", "role": "SWE"})
    assert r["ok"] is False and r["hint"]


def test_search_privacy():
    q = privacy_safe_query("Ledgerly", "sam.rivera@example.com call +1 (415) 555-0100 https://x.y/z", "system design")
    assert "@" not in q and "555" not in q and "http" not in q and "Ledgerly" in q


def test_ics_parse_and_timezone():
    from graph.extraction import normalize_dt

    ev = parse_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;TZID=America/New_York:20261113T140000\nEND:VEVENT\nEND:VCALENDAR")
    assert normalize_dt(ev["start"]) == "2026-11-13T11:00:00-08:00"


def test_check_link_statuses(sandbox):
    from tools.links import check_link

    call = {"name": "check_link", "args": {"urls": ["https://leetcode.com/problems/two-sum/",
                                                     "https://leetcode.com/discuss/interview-question/northwind-old-thread",
                                                     "https://unknown.example.org/x"]}, "id": "c1", "type": "tool_call"}
    cmd = check_link.invoke(call)
    assert list(cmd.update["checked_links"].values()) == ["ok", "broken", "unverified"]


def test_no_send_tool_and_minimal_scopes():
    from graph.curator import CURATOR_TOOLS
    from graph.extraction import EXTRACTION_TOOLS
    from graph.feedback import FEEDBACK_TOOLS
    from graph.triage import TRIAGE_TOOLS
    from tools.google_auth import SCOPES

    names = {t.name for t in TRIAGE_TOOLS + EXTRACTION_TOOLS + CURATOR_TOOLS + FEEDBACK_TOOLS}
    assert not any(w in n for n in names for w in ("send", "forward", "draft", "delete", "compose"))
    assert not any(s.endswith(("gmail.send", "gmail.compose", "mail.google.com/", "/auth/drive")) for s in SCOPES)
    assert "https://www.googleapis.com/auth/drive.file" in SCOPES


def test_reducers_idempotent():
    log = log_reducer([], [{"tool": "a"}])
    assert log_reducer(log, log) == log  # a subgraph returning its full state is a no-op
    qs = questions_reducer([{"id": "1"}, {"id": "2"}], {"add": [{"id": "3"}], "remove": ["1"]})
    assert [q["id"] for q in qs] == ["2", "3"]
