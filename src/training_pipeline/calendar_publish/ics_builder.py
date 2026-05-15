"""Build .ics calendar content from ``weekly_plans`` rows.

Each session in a plan becomes a VEVENT. UIDs are a stable hash of
``(week_of, date, session_type)`` so regenerating the calendar with the same
plan input yields identical UIDs — iOS Calendar therefore updates the existing
events in place rather than duplicating them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ics import Calendar, Event  # type: ignore[import-untyped]

from training_pipeline.shared.models import WeeklyPlan

CALENDAR_TIMEZONE = ZoneInfo("Europe/Oslo")
DEFAULT_START_TIME = time(8, 0)
DEFAULT_DURATION_MINUTES = 60
UID_DOMAIN = "personal-training-mcp"

DATE_KEYS = ("date", "day")
TYPE_KEYS = ("session_type", "type")
NAME_KEYS = ("title", "name", "session_type", "type")
DURATION_KEYS = ("duration_min", "duration", "duration_minutes")


def _first_present(session: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in session and session[key] is not None:
            return session[key]
    return None


def _parse_session_date(value: Any) -> date_type | None:
    if isinstance(value, date_type) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date_type.fromisoformat(value)
        except ValueError:
            return None
    return None


def _parse_session_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(value, fmt).time()
            except ValueError:
                continue
    return DEFAULT_START_TIME


def _parse_duration(value: Any) -> timedelta:
    if isinstance(value, bool):
        return timedelta(minutes=DEFAULT_DURATION_MINUTES)
    if isinstance(value, int | float):
        minutes = int(value)
        if minutes > 0:
            return timedelta(minutes=minutes)
    return timedelta(minutes=DEFAULT_DURATION_MINUTES)


def stable_uid(week_of: date_type, session_date: date_type, session_type: str) -> str:
    """Return a UID that is stable across regenerations for the same session."""
    seed = f"{week_of.isoformat()}|{session_date.isoformat()}|{session_type}".encode()
    digest = hashlib.sha256(seed).hexdigest()[:24]
    return f"{digest}@{UID_DOMAIN}"


def _build_description(session: dict[str, Any]) -> str:
    parts: list[str] = []
    description = session.get("description")
    if isinstance(description, str) and description.strip():
        parts.append(description.strip())
    intensity = session.get("intensity")
    if isinstance(intensity, str) and intensity.strip():
        parts.append(f"Intensity: {intensity.strip()}")
    notes = session.get("notes")
    if isinstance(notes, str) and notes.strip():
        parts.append(f"Notes: {notes.strip()}")
    return "\n".join(parts)


def _session_to_event(plan: WeeklyPlan, session: dict[str, Any]) -> Event | None:
    session_date = _parse_session_date(_first_present(session, DATE_KEYS))
    if session_date is None:
        return None

    type_raw = _first_present(session, TYPE_KEYS)
    session_type = (
        type_raw.strip() if isinstance(type_raw, str) and type_raw.strip() else "Training"
    )

    name_raw = _first_present(session, NAME_KEYS)
    event_name = (
        name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else session_type
    )

    start_time = _parse_session_time(session.get("time"))
    begin = datetime.combine(session_date, start_time, tzinfo=CALENDAR_TIMEZONE)

    event = Event()
    event.name = event_name
    event.begin = begin
    event.duration = _parse_duration(_first_present(session, DURATION_KEYS))
    event.uid = stable_uid(plan.week_of, session_date, session_type)
    description = _build_description(session)
    if description:
        event.description = description
    return event


def build_calendar(plans: Iterable[WeeklyPlan]) -> Calendar:
    calendar = Calendar()
    for plan in plans:
        if not isinstance(plan.plan, list):
            continue
        for session in plan.plan:
            if not isinstance(session, dict):
                continue
            event = _session_to_event(plan, session)
            if event is not None:
                calendar.events.add(event)
    return calendar


def render(plans: Iterable[WeeklyPlan]) -> str:
    return str(build_calendar(plans).serialize())
