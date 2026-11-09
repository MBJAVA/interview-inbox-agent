import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update({"MOCK_GOOGLE": "true", "MOCK_WEB": "true", "MOCK_LLM": "true", "LANGSMITH_TRACING": "false"})


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated data/output dirs, fresh DB connection, default clock."""
    from core import clock, db

    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_OUTPUT_DIR", str(tmp_path / "out"))
    db.reset_connection()
    clock.set_now(None)

    def inbox(*names):
        monkeypatch.setenv("MOCK_INBOX_FILES", ",".join(names))

    yield inbox
    clock.set_now(None)
    db.reset_connection()
