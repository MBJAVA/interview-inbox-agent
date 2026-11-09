"""generate_tailored_question: autonomous fallback (and the only source for ai_assisted_coding)."""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from core.llm import prompt, structured
from graph.schemas import GeneratedQuestion
from tools.common import ok, safe_tool
from tools.question_bank import find_bank_match

MAX_PROMPT_CHARS = 600
# Worked examples and the like: everything from the first such heading on is dropped.
_TAIL = re.compile(r"^\s*(?:\*\*)?(examples?|explanation|sample (?:input|output)|follow[- ]up|hints?)\b", re.I | re.M)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def tidy_prompt(text: str) -> str:
    """One short paragraph: drop worked examples, flatten lists and line breaks."""
    m = _TAIL.search(text)
    if m:
        text = text[: m.start()]
    lines = [_BULLET.sub("", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\s{2,}", " ", " ".join(ln for ln in lines if ln)).strip()


@tool
@safe_tool("Generation failed; try again with a simpler topic, or leave the slot unfilled (it will be reported).")
def generate_tailored_question(round_type: str, level: str, difficulty: str, industry: Optional[str] = None,
                               topic: Optional[str] = None, job_summary: Optional[str] = None) -> dict:
    """Generate one interview question tailored to the round, level, difficulty, industry and topic.
    Use when the bank and company search can't fill a slot, the industry isn't reflected, or the round has no
    public bank (always for ai_assisted_coding). Returns source='generated' and url=null. If the result
    duplicates a bank question, bank_match names it: submit that bank question instead. Do not ask permission."""
    msgs = [
        SystemMessage(prompt("generator")),
        HumanMessage(f"round_type={round_type}\nlevel={level}\ndifficulty={difficulty}\nindustry={industry}\n"
                     f"topic={topic}\njob_summary={(job_summary or '')[:600]}"),
    ]
    gen = structured("generator", GeneratedQuestion)
    g = gen.invoke(msgs)
    text = tidy_prompt(g.prompt)
    if len(text) > MAX_PROMPT_CHARS:  # one retry with feedback, then keep the tidied text
        g = gen.invoke(msgs + [HumanMessage(f"That prompt is {len(text)} characters. Rewrite the same problem in "
                                            "3-4 sentences (under 60 words): Input, Output, Constraints only.")])
        text = tidy_prompt(g.prompt)
    qid = "gen-" + hashlib.sha1(f"{round_type}{difficulty}{g.title}".encode()).hexdigest()[:10]
    question = {"id": qid, "title": g.title, "prompt": text, "round_type": round_type, "topic": g.topic,
                "difficulty": difficulty, "competency": g.competency, "source": "generated",
                "source_name": "Generated for this interview", "url": None}
    bm = find_bank_match(question)
    if bm:
        return ok(question=question, bank_match={**bm, "source": "bank"},
                  note=f"This duplicates bank question '{bm['title']}' ({bm['id']}). Submit the bank version "
                       "(check_link its url first) or generate a different problem; accept_questions rejects the generated copy.")
    return ok(question=question)
