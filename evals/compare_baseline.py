"""Baseline: the same emails through ONE plain LLM call (no tools, no loop, no memory).

Compares classification accuracy, extraction accuracy, link validity, duplicate docs and slot
compliance against the agent's numbers from evals/results.json (run run_evals.py first).

    python evals/compare_baseline.py
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MOCK_GOOGLE", "true")
os.environ.setdefault("MOCK_WEB", "true")

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from core.llm import prompt, structured  # noqa: E402
from evals.run_evals import _norm  # noqa: E402
from evals.scenarios import SCENARIOS  # noqa: E402
from graph import quality  # noqa: E402
from graph.curator import compute_targets  # noqa: E402
from graph.extraction import normalize_dt  # noqa: E402
from graph.schemas import BaselineResult  # noqa: E402
from tools.web import link_status  # noqa: E402

FIELD_MAP = ["company", "role", "level", "round_types", "interview_datetime"]


def baseline_one(msg: dict) -> BaselineResult:
    text = f"From: {msg['from']}\nSubject: {msg['subject']}\nDate: {msg['date']}\nBody:\n{msg['body']}"
    return structured("baseline", BaselineResult).invoke([SystemMessage(prompt("baseline")), HumanMessage(text)])


def main() -> int:
    agent_path = ROOT / "evals" / "results.json"
    if not agent_path.exists():
        print("Run evals/run_evals.py first.")
        return 1
    agent = json.loads(agent_path.read_text())
    def scenario_messages(sc: dict) -> list[dict]:
        out = []
        for name in sc["files"]:
            out += json.loads((ROOT / "fixtures" / "emails" / f"{name}.json").read_text())["messages"]
        return sorted(out, key=lambda m: m["date"])

    cls_ok = cls_n = f_ok = f_n = 0
    links = Counter()
    docs_created = 0
    interviews = set()
    slot_ok = slot_n = 0
    violations = 0
    for sc in SCENARIOS[:20]:
        exp = sc["expect"]
        order = scenario_messages(sc)
        times = 2 if sc["id"] == 20 else 1  # scenario 20: the same email arrives twice
        for _ in range(times):
            for m in order:
                res = baseline_one(m)
                if m["id"] in exp.get("classes", {}):
                    cls_n += 1
                    cls_ok += res.label == exp["classes"][m["id"]]
                interview_like = res.label in ("interview_invite", "scheduling_request", "interview_confirmation", "reschedule", "next_round")
                if interview_like:
                    docs_created += 1  # no memory: every interview-like email becomes a new doc
                    interviews.add(sc["id"])
                for q in res.questions:
                    st = link_status(q.url) if q.url else "none"
                    links[st] += 1
                    violations += len(quality.validate({"round_type": q.round_type, "title": q.title, "prompt": q.title,
                                                        "url": q.url, "source": "bank"}))
                if interview_like and res.round_types:
                    targets = compute_targets({"level": res.level or "senior", "round_types": res.round_types})
                    c = Counter((q.round_type, q.difficulty) for q in res.questions)
                    for r, ds in targets.items():
                        for d, (lo, hi) in ds.items():
                            slot_n += 1
                            slot_ok += lo <= c.get((r, d), 0) <= hi
                last = m["id"] == order[-1]["id"]
                if last and _ == times - 1:
                    for t, want in exp.get("record", {}).items():
                        for k in FIELD_MAP:
                            if k not in want:
                                continue
                            got = getattr(res, k)
                            if k == "interview_datetime":
                                got = normalize_dt(got)
                            f_n += 1
                            f_ok += _norm(got) == _norm(want[k])
    a = agent["summary"]
    total_links = sum(links.values()) or 1
    base = {
        "classification_accuracy": round(cls_ok / max(1, cls_n), 3),
        "extraction_accuracy": round(f_ok / max(1, f_n), 3),
        "links_ok_rate": round(links["ok"] / total_links, 3),
        "links": dict(links),
        "duplicate_docs": docs_created - len(interviews),
        "slot_compliance": round(slot_ok / max(1, slot_n), 3),
        "quality_violations": violations,
    }
    agent_cmp = {
        "classification_accuracy": a["classification_accuracy"], "extraction_accuracy": a["extraction_accuracy"],
        "links_ok_rate": f"every link checked; broken in final docs: {a['broken_links_in_final_docs']}",
        "duplicate_docs": a["duplicate_docs"], "slot_compliance": a["slot_fill_rate"], "quality_violations": a["quality_violations"],
    }
    rows = ["| Metric | Agent | One-shot baseline |", "|---|---|---|"]
    for k in ["classification_accuracy", "extraction_accuracy", "links_ok_rate", "duplicate_docs", "slot_compliance", "quality_violations"]:
        rows.append(f"| {k} | {agent_cmp[k]} | {base[k]} |")
    rows.append(f"\nBaseline link statuses: {base['links']}. Model: {a.get('model')}.")
    md = "\n".join(rows)
    (ROOT / "evals" / "baseline.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
