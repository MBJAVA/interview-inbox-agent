from graph import quality


def q(**kw):
    base = {"id": "x", "title": "T", "prompt": "Input: a list. Output: a number. Constraints: n up to 10^5.", "round_type": "coding",
            "topic": "arrays", "source": "generated", "url": None}
    base.update(kw)
    return base


def test_real_coding_problem_passes():
    assert quality.validate(q()) == []


def test_concept_question_rejected():
    v = quality.validate(q(title="Explain time vs space", prompt="Explain the time vs. space tradeoff."))
    assert any("concept" in x for x in v)


def test_cheatsheet_link_rejected():
    v = quality.validate(q(url="https://example.com/leetcode-patterns-cheatsheet"))
    assert any("cheatsheet" in x for x in v)


def test_bank_coding_must_link_to_problem():
    v = quality.validate(q(source="bank", url="https://neetcode.io/roadmap"))
    assert any("directly to the problem" in x for x in v)


def test_ai_assisted_requires_assistant_and_verification():
    bad = q(round_type="ai_assisted_coding", title="Design a chat system", prompt="Design a chat system for 1M users.")
    assert quality.validate(bad)
    good = q(round_type="ai_assisted_coding", title="Extend a service with an AI assistant",
             prompt="Using an AI coding assistant, add retries; review the diff, run the tests, verify and correct it.")
    assert quality.validate(good) == []


def test_behavioral_needs_competency():
    assert quality.validate(q(round_type="behavioral", prompt="Tell me about yourself.", topic="intro"))
    assert quality.validate(q(round_type="behavioral", prompt="Tell me about a conflict.", topic="conflict")) == []


def test_role_filter():
    v = quality.validate(q(round_type="system_design", prompt="Design a roadmap", audience_role="product_manager"))
    assert any("role filter" in x for x in v)


def test_semantic_dedupe_across_rounds():
    coding = q(id="a", title="Implement a token bucket rate limiter", topic="rate limiting")
    sd = q(id="b", round_type="system_design", title="Design a distributed rate limiter", topic="rate limiting")
    assert quality.is_duplicate(sd, [coding])["id"] == "a"
    assert quality.is_duplicate(q(id="c", title="Ledgerly onsite: reconcile two logs"), [q(id="d", title="Design a payments ledger")]) is None


def test_never_easy_floor():
    d, why = quality.calibration_floor({"title": "Design real-time collaborative editing"}, "easy")
    assert d == "medium" and why
