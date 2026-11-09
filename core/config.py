"""Config + environment loading. One place to read config.yaml and env flags."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def mock_google() -> bool:
    return _flag("MOCK_GOOGLE", "true")


def mock_web() -> bool:
    return _flag("MOCK_WEB", "true")


def mock_llm() -> bool:
    """Offline scripted policy model (tests/CI without an API key). See core/fake_llm.py."""
    return _flag("MOCK_LLM", "false")


@lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    path = Path(os.getenv("AGENT_CONFIG", ROOT / "config.yaml"))
    with open(path) as f:
        return yaml.safe_load(f)


def path(key: str) -> Path:
    p = Path(config()["paths"][key])
    if os.getenv("AGENT_DATA_DIR") and key in {"db", "checkpoints", "lock"}:
        p = Path(os.environ["AGENT_DATA_DIR"]) / p.name
    elif mock_google() and key in {"db", "checkpoints", "lock"}:
        p = p.parent / "mock" / p.name  # mock runs never share state with your real inbox
    if os.getenv("AGENT_OUTPUT_DIR") and key in {"output", "mock_drive"}:
        base = Path(os.environ["AGENT_OUTPUT_DIR"])
        p = base if key == "output" else base / "mock_drive"
    p = p if p.is_absolute() else ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def budget(key: str) -> int:
    return int(config()["budgets"][key])
