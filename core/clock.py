"""Clock with an override so mock runs, the demo, and evals are reproducible."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.config import config, mock_google

_override: datetime | None = None


def set_now(dt: datetime | None) -> None:
    global _override
    _override = dt


def now() -> datetime:
    if _override is not None:
        return _override
    env = os.getenv("AGENT_NOW")
    if env:
        return datetime.fromisoformat(env)
    if mock_google():
        return datetime.fromisoformat(config()["mock"]["now"])
    return datetime.now(timezone.utc)


def user_tz() -> ZoneInfo:
    return ZoneInfo(config()["user"]["timezone"])


def now_local() -> datetime:
    return now().astimezone(user_tz())
