"""Regression tests for the prep-doc review: bank duplicates, calibration references, short prompts,
decision-vs-failure logging, and source labels checked against the email text."""
from graph.extraction import verify_body_source
from graph.output import build_sections
from tools.generator import MAX_PROMPT_CHARS, tidy_prompt
from tools.question_bank import find_bank_match, similar_bank_questions


def test_generated_copy_of_bank_question_is_matched():
    q = {"id": "gen-x", "title": "Longest Substring Without Repeating Characters", "round_type": "coding",
         "topic": "strings", "source": "generated"}
    m = find_bank_match(q)
    assert m and m["url"] and "leetcode.com/problems/" in m["url"]


def test_unrelated_generated_question_has_no_bank_match():
    q = {"id": "gen-y", "title": "Reconcile Merchant Payout Batches", "round_type": "coding", "topic": "arrays"}
    assert find_bank_match(q) is None


def test_similar_bank_questions_give_labeled_references():
    refs = similar_bank_questions({"title": "Shortest word transformation", "round_type": "coding", "topic": "graphs/bfs"})
    assert refs and all({"title", "difficulty"} <= r.keys() for r in refs)


def test_tidy_prompt_drops_examples_and_flattens():
    raw = ("Given an array of integers, sort by frequency.\n\nInput: An integer array nums.\nOutput: The sorted array.\n\n"
           "Constraints:\n- 1 <= nums.length <= 10^5\n\nExample:\nInput: [4, 5, 6]\nOutput: [4, 5, 6]\n\nExplanation: ...")
    out = tidy_prompt(raw)
    assert "Example" not in out and "Explanation" not in out and "\n" not in out
    assert "Constraints: 1 <= nums.length <= 10^5" in out
    assert len(out) <= MAX_PROMPT_CHARS


def test_waiting_decision_is_not_listed_as_failure():
    state = {"interview": {"company": "PayPal"}, "questions": [], "build_log": [
        {"tool": "extraction", "outcome": "waiting: time not set; building the prep doc early", "fallback": None},
        {"tool": "curator:check_link", "outcome": "failed: timeout", "fallback": "model decided next step"}]}
    built = dict(build_sections(state))["How this was built"]
    failures, steps = built.split("- Recent steps:")
    assert "check_link" in failures and "waiting" not in failures and "waiting" in steps


def test_body_source_relabeled_when_value_only_in_sender_domain():
    email = {"sender": "Corine McEntee <corine@talent.paypal.com>", "subject": "Scheduling your Karat interview",
             "body": "Hi, please share times for a 60-minute Karat interview. Thanks, Corine McEntee"}
    assert verify_body_source("PayPal", "email body", email) == "sender domain (talent.paypal.com)"
    assert verify_body_source("Corine McEntee", "email body", email) == "email body"
    assert verify_body_source("Karat", "email body", email) == "email body"
    assert verify_body_source("Acme", "email body", email).startswith("inferred")
    assert verify_body_source("PayPal", "thread message 1", email) == "thread message 1"


def test_coverage_loop_drops_tool_calls_cut_off_by_step_cap():
    from langchain_core.messages import AIMessage, SystemMessage

    from graph.curator import coverage_check

    cut_off = AIMessage(content="", id="ai-1", tool_calls=[{"name": "search_question_bank", "args": {}, "id": "call_1"}])
    state = {"messages": [SystemMessage("x"), cut_off], "questions": [], "slot_targets": {"coding": {"hard": [1, 1]}},
             "iteration_count": 0, "max_iterations": 5, "last_question_count": -1}
    state["interview"] = {"company": "Acme", "round_types": ["coding"]}
    upd = coverage_check(state)
    assert upd["next_step"] == "loop"
    # The next pass starts fresh: history cleared, no AI message with unanswered tool calls carried over.
    assert type(upd["messages"][0]).__name__ == "RemoveMessage"
    assert not any(getattr(m, "tool_calls", None) for m in upd["messages"])


def test_level_normalization():
    from graph.extraction import normalize_level

    assert normalize_level("II") == "mid" and normalize_level("Sr.") == "senior" and normalize_level("L6") == "staff"
    assert normalize_level("Senior Backend Engineer") == "senior"
    assert normalize_level("Engineer") is None and normalize_level(None) is None


def test_markdown_to_docs_styles_land_on_the_right_text():
    from tools.drive_docs import markdown_to_docs

    md = ("# PayPal – SWE – Coding\n## Questions\n#### Hard\n"
          "- **Word Ladder** — [Open on LeetCode](https://leetcode.com/problems/word-ladder/)\n"
          "  - Input: beginWord. Output: steps.\n  - _Topic: graphs · Source: Blind 75_\n"
          "## Interview details\n- **Company:** PayPal _(source: sender domain)_\nPlain paragraph with round_types kept.")
    text, reqs = markdown_to_docs(md, start=1)
    assert "**" not in text and "](" not in text and "_(" not in text and "round_types" in text

    def span(r):
        rg = r["range"]
        return text[rg["startIndex"] - 1:rg["endIndex"] - 1]

    styled = {(span(r["updateTextStyle"]), r["updateTextStyle"]["fields"])
              for r in reqs if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"]}
    assert ("Word Ladder", "bold") in styled and ("Company:", "bold") in styled
    assert ("Open on LeetCode", "link") in styled
    assert ("(source: sender domain)", "italic,foregroundColor") in styled
    styles = {span(r["updateParagraphStyle"]).strip(): r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
              for r in reqs if "updateParagraphStyle" in r}
    assert styles["PayPal – SWE – Coding"] == "HEADING_1" and styles["Hard"] == "HEADING_4"
    bullets = [r["createParagraphBullets"]["range"] for r in reqs if "createParagraphBullets" in r]
    assert len(bullets) == 2 and bullets[0]["startIndex"] > bullets[1]["startIndex"]  # bottom-up
    assert "\tInput: beginWord" in text  # nested bullet via leading tab


def test_question_block_is_readable():
    from graph.output import question_block

    out = question_block({"title": "Word Ladder", "prompt": "Input: words.", "topic": "graphs/bfs", "difficulty": "hard",
                          "difficulty_reason": "bank label (Blind 75), suited to senior", "source": "bank",
                          "source_name": "Blind 75", "url": "https://leetcode.com/problems/word-ladder/", "link_status": "ok"})
    assert "link status" not in out and "`" not in out and "Why hard" not in out
    assert out.splitlines()[0] == "- **Word Ladder** — [Open on LeetCode](https://leetcode.com/problems/word-ladder/)"


def test_italic_with_inner_underscores():
    from tools.drive_docs import markdown_to_docs

    text, _ = markdown_to_docs("- **Date:** Fri  _(source: email body, converted to America/Los_Angeles)_")
    assert "_(" not in text and ")_" not in text and "Los_Angeles" in text


def test_company_search_requires_the_source_to_name_the_company():
    from tools.company_questions import mentions_company

    assert mentions_company("Orbitline Logistics", "Orbitline onsite: design a dispatch queue")
    assert mentions_company("PayPal", "https://www.reddit.com/r/cscareerquestions/paypal_karat_interview")
    assert not mentions_company("Orbitline Logistics", "System design interview question from Netflix")


def test_leetcode_links_use_the_problem_lookup(monkeypatch):
    from tools import web

    monkeypatch.setattr(web, "mock_web", lambda: False)
    seen = []
    monkeypatch.setattr(web, "_leetcode_status", lambda slug: seen.append(slug) or "ok")
    assert web.link_status("https://leetcode.com/problems/word-ladder/") == "ok" and seen == ["word-ladder"]


def test_mock_runs_use_a_separate_database(monkeypatch):
    from core import config

    monkeypatch.delenv("AGENT_DATA_DIR", raising=False)
    monkeypatch.setenv("MOCK_GOOGLE", "true")
    assert config.path("db").parent.name == "mock"
    monkeypatch.setenv("MOCK_GOOGLE", "false")
    assert config.path("db").parent.name == "data"
