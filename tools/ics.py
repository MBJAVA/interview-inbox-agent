"""Minimal .ics (text/calendar) parser: DTSTART/DTEND with TZID, UTC 'Z', or floating times."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(params: str, value: str) -> tuple[str | None, str | None]:
    tzid = None
    m = re.search(r"TZID=([^;:]+)", params)
    if m:
        tzid = m.group(1).strip('"')
    value = value.strip()
    try:
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.isoformat(), "UTC"
        fmt = "%Y%m%dT%H%M%S" if "T" in value else "%Y%m%d"
        dt = datetime.strptime(value, fmt)
        if tzid:
            dt = dt.replace(tzinfo=ZoneInfo(tzid))
        return dt.isoformat(), tzid
    except Exception:
        return None, tzid


def parse_ics(text: str) -> dict | None:
    if "BEGIN:VEVENT" not in text:
        return None
    event: dict = {}
    in_event = False
    for line in _unfold(text):
        if line.startswith("BEGIN:VEVENT"):
            in_event = True
            continue
        if line.startswith("END:VEVENT"):
            break
        if not in_event or ":" not in line:
            continue
        key_params, value = line.split(":", 1)
        key, _, params = key_params.partition(";")
        key = key.upper()
        if key == "DTSTART":
            event["start"], event["timezone"] = _parse_dt(params, value)
        elif key == "DTEND":
            event["end"], _ = _parse_dt(params, value)
        elif key == "SUMMARY":
            event["summary"] = value
        elif key == "LOCATION":
            event["location"] = value
        elif key == "DESCRIPTION":
            event["description"] = value.replace("\\n", "\n")
    return event or None
