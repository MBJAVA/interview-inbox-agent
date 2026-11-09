"""Run the 24 fixture scenarios in mock mode and report metrics.

    python evals/run_evals.py            # writes evals/results.json and evals/results.md

Uses the real model if MODEL_* env vars are set and MOCK_LLM is false; otherwise set
MOCK_LLM=true to run with the offline policy model (CI / no API key).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MOCK_GOOGLE", "true")
os.environ.setdefault("MOCK_WEB", "true")

from core import clock, db  # noqa: E402
from evals.scenarios import SCENARIOS  # noqa: E402

FIELDS = ["company", "role", "level", "round_types", "interview_datetime", "format", "status"]
REGISTERED = {"get_thread", "search_inbox", "get_interview_record", "get_message", "fetch_job_post", "research_company_process",
              "search_question_bank", "search_company_questions", "generate_tailored_question", "check_link",
              "accept_questions", "remove_questions", "get_question_details", "read_attachment"}


def _isolate(files: list[str]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="eval_"))
    os.environ.update({"AGENT_DATA_DIR": str(tmp), "AGENT_OUTPUT_DIR": str(tmp / "out"), "MOCK_INBOX_FILES": ",".join(files)})
    db.reset_connection()
    clock.set_now(None)
    return tmp


def _record_for_thread(t: str) -> dict | None:
    return db.record_for_gmail_thread(t)


def _docs() -> list[dict]:
    from tools.mocks import drive_mock

    return drive_mock.list_docs()


def _norm(v):
    if isinstance(v, list):
        return sorted(v)
    if isinstance(v, str):
        return v.strip().lower()
    return v


def quality_report(questions: list[dict], targets: dict | None) -> dict:
    from graph import quality

    violations = []
    for i, q in enumerate(questions):
        for v in quality.validate(q):
            violations.append(f"{q['id']}: {v}")
        dup = quality.is_duplicate(q, questions[:i])
        if dup:
            violations.append(f"{q['id']}: duplicate of {dup['id']}")
    broken = sum(1 for q in questions if q.get("link_status") == "broken")
    over_cap = []
    if targets:
        c = Counter((q["round_type"], q["difficulty"]) for q in questions)
        for (r, d), n in c.items():
            hi = targets.get(r, {}).get(d, [0, 0])[1]
            if n > hi:
                over_cap.append(f"{r}/{d}: {n}>{hi}")
    return {"violations": violations, "broken_links": broken, "over_cap": over_cap}


def slot_fill(rec: dict) -> tuple[int, int]:
    from graph.curator import compute_targets

    targets = compute_targets(rec["interview"])
    c = Counter((q["round_type"], q["difficulty"]) for q in rec.get("questions", []))
    filled = sum(min(c.get((r, d), 0), hi) for r, ds in targets.items() for d, (lo, hi) in ds.items())
    target = sum(hi for ds in targets.values() for lo, hi in ds.values())
    return filled, target


def run_scenario(sc: dict) -> dict:
    from core import runner
    from tools.mocks import gmail_mock

    tmp = _isolate(sc["files"])
    out = {"id": sc["id"], "name": sc["name"], "checks": {}, "emails": [], "errors": []}
    snapshot = None
    classes: dict = {}
    handoffs: list = []
    fb_class = None
    try:
        for step in sc["steps"]:
            kind = step[0]
            if kind in ("run", "run_at"):
                clock.set_now(datetime.fromisoformat(step[1]) if kind == "run_at" else None)
                res = runner.run_once(include_comments=False)
                for r in res["emails"]:
                    out["emails"].append({"message_id": r.get("message_id"), "status": r["status"], "seconds": r.get("seconds"),
                                          "path_len": len(r.get("tool_path", [])),
                                          "path": [t.split("#")[0] for t in r.get("tool_path", [])]})
                    if r.get("classification"):
                        classes[r["message_id"]] = r["classification"]["label"]
                    if r["status"] == "needs_input":
                        handoffs.append(r)
                        cls_state = runner.get_app().get_state({"configurable": {"thread_id": f"gt_{gmail_mock.get_message_raw(r['message_id'])['thread_id']}"}})
                        c = (cls_state.values.get("classification") or {}).get("label")
                        if c:
                            classes[r["message_id"]] = c
                    if r["status"] == "error":
                        out["errors"].append(r["error"])
                for r in res["expired_handoffs"]:
                    out["emails"].append({"message_id": "(timeout)", "status": r["status"], "path_len": len(r.get("tool_path", [])),
                                          "path": [t.split("#")[0] for t in r.get("tool_path", [])], "seconds": None})
                clock.set_now(None)
            elif kind == "force":
                r = runner.process_email(step[1], force=True)
                out["emails"].append({"message_id": step[1], "status": r["status"], "seconds": r.get("seconds"),
                                      "path_len": len(r.get("tool_path", [])), "path": [t.split("#")[0] for t in r.get("tool_path", [])]})
            elif kind == "snapshot":
                rec = db.list_records()[0]
                snapshot = {"record": json.loads(json.dumps(rec)), "docs": json.loads(json.dumps(_docs()))}
            elif kind == "refine":
                rid = db.list_records()[0]["id"]
                r = runner.run_feedback(rid, step[1], "cli")
                fb_class = r.get("classification")
                out["feedback_line"] = r.get("line")
                out["emails"].append({"message_id": "(feedback)", "status": r["status"], "path_len": len(r.get("tool_path", [])),
                                      "path": [t.split("#")[0] for t in r.get("tool_path", [])], "seconds": None})
            elif kind == "comments":
                out["comments"] = runner.process_comments()
            elif kind == "rebuild":
                runner.rebuild(db.list_records()[0]["id"])
        exp = sc["expect"]
        # classification
        out["classification"] = {m: {"expected": lab, "got": classes.get(m), "ok": classes.get(m) == lab} for m, lab in exp.get("classes", {}).items()}
        # extraction
        fields = {}
        for t, want in exp.get("record", {}).items():
            rec = _record_for_thread(t) or {}
            got = rec.get("interview", {})
            for k, v in want.items():
                fields[f"{t}.{k}"] = {"expected": v, "got": got.get(k), "ok": _norm(got.get(k)) == _norm(v)}
        out["fields"] = fields
        # handoffs
        pend = db.list_pending("pending") + db.list_pending("timed_out") + db.list_pending("answered")
        triggers = [h["payload"].get("trigger") for h in pend]
        want_h = exp.get("handoff")
        out["handoff"] = {"expected": want_h, "got": triggers, "ok": (want_h in triggers) if want_h else not triggers,
                          "unnecessary": len(triggers) if not want_h else max(0, len(triggers) - 1)}
        # docs and duplicates
        docs = _docs()
        recs = db.list_records()
        dup = len(docs) - len({r["interview"].get("doc_id") for r in recs if r.get("interview", {}).get("doc_id")})
        out["docs"] = {"expected": exp.get("docs"), "got": len(docs), "ok": len(docs) == exp.get("docs"), "duplicates": max(0, dup)}
        # quality, links, slots
        from graph.curator import compute_targets

        q_all, viol, broken, fill, target = [], [], 0, 0, 0
        for r in recs:
            qs = r.get("questions", [])
            if not qs:
                continue
            qr = quality_report(qs, compute_targets(r["interview"]))
            viol += qr["violations"] + qr["over_cap"]
            broken += qr["broken_links"]
            f, tg = slot_fill(r)
            fill += f
            target += tg
            q_all += qs
        out["quality_violations"] = viol
        out["broken_links"] = broken
        out["slot_fill"] = {"filled": fill, "target": target}
        if fb_class is not None:
            out["feedback"] = {"expected": exp.get("feedback"), "got": fb_class, "ok": fb_class == exp.get("feedback")}
        # extra checks
        for chk in exp.get("checks", []):
            out["checks"][chk] = _check(chk, out, snapshot, recs, docs)
    except Exception as e:  # report, don't crash the suite
        import traceback

        out["errors"].append(f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")
    finally:
        clock.set_now(None)
        shutil.rmtree(tmp, ignore_errors=True)
    parts = [all(v["ok"] for v in out.get("classification", {}).values()),
             all(v["ok"] for v in out.get("fields", {}).values()),
             out.get("handoff", {}).get("ok", False), out.get("docs", {}).get("ok", False),
             out.get("docs", {}).get("duplicates", 1) == 0, not out.get("quality_violations"), out.get("broken_links", 1) == 0,
             all(out["checks"].values()), not out["errors"], out.get("feedback", {"ok": True})["ok"]]
    out["pass"] = all(parts)
    return out


def _check(chk: str, out: dict, snap: dict | None, recs: list[dict], docs: list[dict]) -> bool:
    from tools.mocks import drive_mock, gmail_mock

    name, _, arg = chk.partition(":")
    paths = [p for e in out["emails"] for p in e["path"]]
    rec = recs[0] if recs else {}
    if name == "path_has":
        return any(p.endswith(":" + arg) for p in paths)
    if name == "label_waiting":
        return any("Interview Prep/Waiting" in gmail_mock.get_labels(m["id"]) for m in gmail_mock.all_messages())
    if name == "label_needs_input":
        return any("Interview Prep/Needs Input" in gmail_mock.get_labels(m["id"]) for m in gmail_mock.all_messages())
    if name == "label_processed":
        return all("Interview Prep/Processed" in gmail_mock.get_labels(m["id"]) for m in gmail_mock.all_messages())
    if name == "no_records":
        return not recs
    if name == "one_record":
        return len(recs) == 1
    if name == "only_registered_tools":
        return all(p.split(":")[1] in REGISTERED for p in paths)
    if name == "questions_unchanged_since_snapshot":
        return sorted(q["id"] for q in rec.get("questions", [])) == sorted(q["id"] for q in snap["record"].get("questions", []))
    if name == "same_doc_since_snapshot":
        return rec["interview"].get("doc_id") == snap["record"]["interview"].get("doc_id") and len(docs) == 1
    if name == "all_questions_round":
        return bool(rec.get("questions")) and all(q["round_type"] == arg for q in rec["questions"])
    if name == "handoff_mentions":
        return any(arg in h["question"] for h in db.list_pending("pending"))
    if name == "has_generated":
        r, d = arg.split(":")
        return any(q["source"] == "generated" and q["round_type"] == r and q["difficulty"] == d for q in rec.get("questions", []))
    if name == "no_unfilled":
        return not rec.get("unfilled_slots")
    if name == "open_question_mentions":
        return any(arg in q for r in recs for q in r.get("open_questions", []))
    if name == "no_questions":
        r = db.record_for_gmail_thread(arg) or {}
        return not r.get("questions")
    if name == "field_nonempty":
        t, f = arg.split(":")
        return bool(((db.record_for_gmail_thread(t) or {}).get("interview") or {}).get(f))
    if name == "guessed":
        t, f = arg.split(":")
        r = db.record_for_gmail_thread(t) or {}
        return f in (r.get("guessed_fields") or [])
    if name == "only_round_changed":
        before = {q["id"] for q in snap["record"]["questions"] if q["round_type"] != arg}
        after = {q["id"] for q in rec["questions"] if q["round_type"] != arg}
        changed = {q["id"] for q in snap["record"]["questions"] if q["round_type"] == arg} != {q["id"] for q in rec["questions"] if q["round_type"] == arg}
        return before == after and changed
    if name == "no_easy":
        return not any(q["round_type"] == arg and q["difficulty"] == "easy" for q in rec["questions"])
    if name == "revision_logged":
        return len(rec.get("revision_history", [])) > len(snap["record"].get("revision_history", []))
    if name == "slot_count":
        r, d, n = arg.split(":")
        return sum(1 for q in rec["questions"] if q["round_type"] == r and q["difficulty"] == d) == int(n)
    if name == "override_stored":
        return rec["interview"].get("count_overrides") == {"system_design:hard": 5}
    if name == "comment_replied":
        st = drive_mock._comment_state()
        return bool(st["replies"].get(arg)) and arg in st["resolved"]
    if name == "comment_not_replied":
        return not drive_mock._comment_state()["replies"].get(arg)
    if name == "doc_unchanged_since_snapshot":
        return [d["agent_sections"] for d in docs] == [d["agent_sections"] for d in snap["docs"]]
    raise ValueError(f"unknown check {chk}")


def summarize(results: list[dict]) -> dict:
    cls = [c for r in results for c in r.get("classification", {}).values()]
    flds = [f for r in results for f in r.get("fields", {}).values()]
    per_field: dict = {}
    for r in results:
        for k, v in r.get("fields", {}).items():
            f = k.split(".", 1)[1]
            per_field.setdefault(f, []).append(v["ok"])
    times = [e["seconds"] for r in results for e in r["emails"] if e.get("seconds")]
    paths = [e["path_len"] for r in results for e in r["emails"]]
    fill = sum(r.get("slot_fill", {}).get("filled", 0) for r in results)
    target = sum(r.get("slot_fill", {}).get("target", 0) for r in results)
    return {
        "scenarios_passed": sum(r["pass"] for r in results), "scenarios": len(results),
        "classification_accuracy": round(sum(c["ok"] for c in cls) / max(1, len(cls)), 3),
        "extraction_accuracy": round(sum(f["ok"] for f in flds) / max(1, len(flds)), 3),
        "per_field_accuracy": {k: round(sum(v) / len(v), 3) for k, v in per_field.items()},
        "handoffs_correct": sum(1 for r in results if r.get("handoff", {}).get("expected") and r["handoff"]["ok"]),
        "handoffs_expected": sum(1 for r in results if r.get("handoff", {}).get("expected")),
        "unnecessary_handoffs": sum(r.get("handoff", {}).get("unnecessary", 0) for r in results),
        "duplicate_docs": sum(r.get("docs", {}).get("duplicates", 0) for r in results),
        "slot_fill_rate": round(fill / max(1, target), 3),
        "quality_violations": sum(len(r.get("quality_violations", [])) for r in results),
        "broken_links_in_final_docs": sum(r.get("broken_links", 0) for r in results),
        "avg_seconds_per_email": round(sum(times) / max(1, len(times)), 3), "max_seconds_per_email": max(times or [0]),
        "avg_tool_path_len": round(sum(paths) / max(1, len(paths)), 2), "max_tool_path_len": max(paths or [0]),
        "distinct_tool_paths": len({tuple(e["path"]) for r in results for e in r["emails"]}),
    }


def to_markdown(results: list[dict], summary: dict) -> str:
    lines = ["| # | Scenario | Pass | Class | Fields | Handoff | Docs (dup) | Slots | Violations | Broken | Path len |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        c = r.get("classification", {})
        f = r.get("fields", {})
        lines.append(
            f"| {r['id']} | {r['name']} | {'✅' if r['pass'] else '❌'} | {sum(v['ok'] for v in c.values())}/{len(c)} | "
            f"{sum(v['ok'] for v in f.values())}/{len(f)} | {'✓' if r.get('handoff', {}).get('ok') else '✗'} {r.get('handoff', {}).get('got') or ''} | "
            f"{r.get('docs', {}).get('got')} ({r.get('docs', {}).get('duplicates')}) | {r.get('slot_fill', {}).get('filled')}/{r.get('slot_fill', {}).get('target')} | "
            f"{len(r.get('quality_violations', []))} | {r.get('broken_links')} | {max([e['path_len'] for e in r['emails']] or [0])} |")
    lines += ["", "```json", json.dumps(summary, indent=1), "```"]
    return "\n".join(lines)


def main() -> int:
    from core.config import mock_llm

    started = time.time()
    results = [run_scenario(sc) for sc in SCENARIOS]
    summary = summarize(results)
    summary["model"] = "offline policy model (MOCK_LLM)" if mock_llm() else os.getenv("MODEL_NAME", "gpt-6.1-terra")
    summary["wall_seconds"] = round(time.time() - started, 1)
    (ROOT / "evals" / "results.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1, default=str))
    md = to_markdown(results, summary)
    (ROOT / "evals" / "results.md").write_text(md)
    print(md)
    for r in results:
        if not r["pass"]:
            print(f"\n--- FAIL #{r['id']} {r['name']}")
            for k in ("classification", "fields", "handoff", "docs", "feedback"):
                bad = r.get(k)
                if isinstance(bad, dict) and bad and not bad.get("ok", True):
                    print(f"  {k}: {bad}")
                elif isinstance(bad, dict):
                    for kk, vv in bad.items():
                        if isinstance(vv, dict) and not vv.get("ok", True):
                            print(f"  {k}.{kk}: expected {vv['expected']!r} got {vv['got']!r}")
            for k, v in r["checks"].items():
                if not v:
                    print(f"  check failed: {k}")
            for e in r["errors"]:
                print(f"  error: {e}")
            if r.get("quality_violations"):
                print(f"  violations: {r['quality_violations']}")
    return 0 if summary["scenarios_passed"] == summary["scenarios"] else 1


if __name__ == "__main__":
    sys.exit(main())
