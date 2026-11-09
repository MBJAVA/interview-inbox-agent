#!/usr/bin/env python
"""Append questions to data/questions.json with validation.

    python scripts/add_questions.py new_questions.json      # a JSON list of question objects
    python scripts/add_questions.py --id cod-x --title ... --prompt ... --round coding --topic ... \
        --difficulty medium --levels mid senior --source-name "Grind 75" --url https://leetcode.com/problems/x/

Schema: id, title, prompt, round_type, topic, difficulty, levels, industry_tags, source_name, url.
Titles, short prompts and links only; no solutions. Use url=null unless you're sure the link is right.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from graph import quality  # noqa: E402

BANK = ROOT / "data" / "questions.json"
REQUIRED = ["id", "title", "prompt", "round_type", "topic", "difficulty", "levels", "source_name"]


def check(q: dict, existing: list[dict]) -> list[str]:
    errs = [f"missing {k}" for k in REQUIRED if not q.get(k)]
    if q.get("round_type") == "ai_assisted_coding":
        errs.append("ai_assisted_coding has no public bank; those questions are always generated")
    if q.get("difficulty") not in ("easy", "medium", "hard"):
        errs.append("difficulty must be easy/medium/hard")
    if any(e["id"] == q.get("id") for e in existing):
        errs.append("duplicate id")
    errs += quality.validate({**q, "source": "bank", "competency": q.get("topic")})
    dup = quality.is_duplicate(q, [e for e in existing if e["round_type"] == q.get("round_type")])
    if dup:
        errs.append(f"looks like a duplicate of {dup['id']}")
    return errs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("file", nargs="?")
    for a in ("id", "title", "prompt", "round", "topic", "difficulty", "source-name", "url"):
        p.add_argument(f"--{a}")
    p.add_argument("--levels", nargs="*", default=[])
    p.add_argument("--industry-tags", nargs="*", default=[])
    args = p.parse_args()
    if args.file:
        new = json.loads(Path(args.file).read_text())
    else:
        new = [{"id": args.id, "title": args.title, "prompt": args.prompt, "round_type": args.round, "topic": args.topic,
                "difficulty": args.difficulty, "levels": args.levels, "industry_tags": args.industry_tags,
                "source_name": args.source_name, "url": args.url}]
    bank = json.loads(BANK.read_text())
    added = 0
    for q in new:
        q.setdefault("industry_tags", [])
        q.setdefault("url", None)
        errs = check(q, bank)
        if errs:
            print(f"✗ {q.get('id')}: {'; '.join(errs)}")
            continue
        bank.append(q)
        added += 1
        print(f"✓ {q['id']}")
    BANK.write_text(json.dumps(bank, indent=2) + "\n")
    print(f"Added {added}/{len(new)}. Bank size: {len(bank)}")
    return 0 if added == len(new) else 1


if __name__ == "__main__":
    sys.exit(main())
