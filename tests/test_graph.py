"""Integration tests through the real parent graph (mock Google/web, offline policy model)."""
from datetime import datetime

import pytest


def test_interrupt_survives_restart_and_resumes(sandbox):
    sandbox("s05_agency_no_client")
    from core import db, runner
    from graph import parent_graph

    r = runner.run_once(include_comments=False)
    assert r["emails"][0]["status"] == "needs_input"
    assert db.list_pending()[0]["payload"]["trigger"] == "unknown_company"
    # "restart": drop cached connections/graph and resume from SQLite only
    db.reset_connection()
    parent_graph._saver.cache_clear()
    out = runner.answer("gt_t05", "The company is Finlytic")
    assert out["status"] == "done"
    rec = db.record_for_gmail_thread("t05")
    assert rec["interview"]["company"] == "Finlytic" and rec["interview"]["doc_id"]


def test_handoff_timeout_proceeds_with_marked_guess(sandbox):
    sandbox("s18_handoff_timeout")
    from core import clock, db, runner

    runner.run_once(include_comments=False)
    clock.set_now(datetime.fromisoformat("2026-11-05T20:00:00-08:00"))
    assert runner.process_expired_handoffs() == []  # not yet
    clock.set_now(datetime.fromisoformat("2026-11-06T10:00:00-08:00"))
    out = runner.process_expired_handoffs()
    assert out and out[0]["status"] == "done"
    rec = db.record_for_gmail_thread("t18")
    assert "company" in rec["guessed_fields"] and rec["interview"]["status"] == "active"


def test_lock_prevents_overlapping_runs(sandbox):
    from core import runner

    with runner.run_lock():
        with pytest.raises(runner.LockBusy):
            with runner.run_lock():
                pass


def test_tool_budget_is_enforced_and_reported(sandbox, monkeypatch):
    sandbox("s06_multi_round_onsite")
    import copy

    from core import config as cfg
    from core import db, runner

    tight = copy.deepcopy(cfg.config())
    tight["budgets"]["per_email_tool_calls"] = 6
    tight["budgets"]["max_tool_calls"] = 6  # ceiling too, or curation scales the budget up for the 4 rounds
    monkeypatch.setattr(cfg, "config", lambda: tight)
    runner.run_once(include_comments=False)
    rec = db.record_for_gmail_thread("t06")
    assert rec is not None
    assert any("budget" in q for q in rec["open_questions"] + [str(e) for e in rec["build_log"]])
    assert rec["unfilled_slots"], "budget hit must surface unfilled slots, never silently"


def test_transient_doc_error_is_retried(sandbox, monkeypatch):
    sandbox("s20_duplicate")
    from core import db, runner
    from graph import parent_graph
    from tools.common import TransientError

    real = parent_graph.drive_write
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError("503 from Docs")
        return real(*a, **k)

    monkeypatch.setattr(parent_graph, "drive_write", flaky)
    runner.run_once(include_comments=False)
    assert calls["n"] == 2 and db.record_for_gmail_thread("t20")["interview"]["doc_id"]


def test_my_notes_never_modified(sandbox):
    sandbox("s07_reschedule_date_only")
    import json

    from core import clock, db, runner
    from tools.mocks import drive_mock

    clock.set_now(datetime.fromisoformat("2026-11-01T09:00:00-08:00"))
    runner.run_once(include_comments=False)
    doc = drive_mock.list_docs()[0]
    doc["my_notes"] = "my own prep notes"
    (drive_mock._dir() / f"{doc['doc_id']}.json").write_text(json.dumps(doc))
    clock.set_now(None)
    runner.run_once(include_comments=False)
    after = drive_mock.get_doc(doc["doc_id"])
    assert after["my_notes"] == "my own prep notes" and after["update_count"] == 1
    assert len(drive_mock.list_docs()) == 1 and db.record_for_gmail_thread("t07")["interview"]["doc_id"] == doc["doc_id"]


def test_injection_email_changes_nothing(sandbox):
    sandbox("s16_prompt_injection")
    from core import db, runner
    from tools.mocks import drive_mock

    r = runner.run_once(include_comments=False)["emails"][0]
    assert r["classification"]["label"] == "not_relevant"
    assert not db.list_records() and not drive_mock.list_docs()
