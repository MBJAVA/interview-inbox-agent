"""Model factory.

Real runs: `init_chat_model` with provider + model from env (default gpt-6.1-terra).
Offline runs (MOCK_LLM=true): a scripted policy model with the same interface
(bind_tools / with_structured_output) so the whole graph, demo, evals and tests run
with no API key. See core/fake_llm.py.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from core.config import ROOT, mock_llm

PROMPTS = ROOT / "prompts"


@lru_cache(maxsize=None)
def prompt(name: str) -> str:
    base = (PROMPTS / "_safety.md").read_text()
    return base + "\n\n" + (PROMPTS / f"{name}.md").read_text()


def get_model(purpose: str):
    """purpose identifies the agent/call (triage, extraction, curator, ...). Real models ignore it;
    the offline policy model uses it to pick its policy."""
    if mock_llm():
        from core.fake_llm import FakePolicyModel

        return FakePolicyModel(purpose=purpose)
    return _real_model()


@lru_cache(maxsize=1)
def _real_model():
    from langchain.chat_models import init_chat_model

    model = os.getenv("MODEL_NAME", "gpt-6.1-terra")
    provider = os.getenv("MODEL_PROVIDER", "openai")
    return init_chat_model(model, model_provider=provider, temperature=0, timeout=60, max_retries=2)


def structured(purpose: str, schema):
    return get_model(purpose).with_structured_output(schema)


def usage_tokens(msg) -> int:
    meta = getattr(msg, "usage_metadata", None) or {}
    return int(meta.get("total_tokens", 0) or 0)
