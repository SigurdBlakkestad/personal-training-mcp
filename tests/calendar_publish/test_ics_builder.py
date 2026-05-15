"""Unit tests for ics_builder."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

from training_pipeline.calendar_publish.ics_builder import (
    CALENDAR_TIMEZONE,
    build_calendar,
    render,
    stable_uid,
)
from training_pipeline.shared.models import WeeklyPlan


def _plan(week_of: date, sessions: list[dict[str, Any]]) -> WeeklyPlan:
    plan = WeeklyPlan(
        week_of=week_of,
        version=1,
        plan=sessions,
        notes="",
        is_current=True,
    )
    plan.id = uuid4()
    return plan


def test_build_calendar_produces_event_per_session() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [
            {
                "date": "2026-05-18",
                "session_type": "Cycling",
                "description": "Endurance ride",
                "duration_min": 90,
                "intensity": "Easy",
                "time": "06:30",
                "notes": "Zone 2 only",
            },
            {
                "date": "2026-05-20",
                "session_type": "Lifting",
                "description": "Upper push",
                "duration_min": 60,
                "intensity": "Hard",
            },
        ],
    )
    calendar = build_calendar([plan])
    events = list(calendar.events)
    assert len(events) == 2
    names = sorted(e.name for e in events)
    assert names == ["Cycling", "Lifting"]


def test_uid_is_stable_across_regenerations() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [
            {
                "date": "2026-05-18",
                "session_type": "Cycling",
                "duration_min": 60,
            }
        ],
    )
    first = list(build_calendar([plan]).events)[0]
    second = list(build_calendar([plan]).events)[0]
    assert first.uid == second.uid
    assert first.uid == stable_uid(date(2026, 5, 18), date(2026, 5, 18), "Cycling")
    assert first.uid.endswith("@personal-training-mcp")


def test_uid_changes_when_session_type_changes() -> None:
    week = date(2026, 5, 18)
    session_date = date(2026, 5, 18)
    assert stable_uid(week, session_date, "Cycling") != stable_uid(week, session_date, "Lifting")


def test_default_start_time_is_eight_am_local() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [{"date": "2026-05-18", "session_type": "Cycling", "duration_min": 45}],
    )
    event = next(iter(build_calendar([plan]).events))
    assert event.begin.hour == 8
    assert event.begin.minute == 0
    assert event.begin.tzinfo == CALENDAR_TIMEZONE


def test_time_override_is_honored() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [
            {
                "date": "2026-05-18",
                "session_type": "Cycling",
                "duration_min": 45,
                "time": "18:30",
            }
        ],
    )
    event = next(iter(build_calendar([plan]).events))
    assert event.begin.hour == 18
    assert event.begin.minute == 30


def test_sessions_without_date_are_skipped() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [
            {"session_type": "Cycling", "duration_min": 60},
            {"date": "2026-05-19", "session_type": "Lifting", "duration_min": 60},
        ],
    )
    events = list(build_calendar([plan]).events)
    assert len(events) == 1
    assert events[0].name == "Lifting"


def test_non_dict_sessions_are_skipped() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [
            "not-a-dict",  # type: ignore[list-item]
            {"date": "2026-05-19", "session_type": "Lifting", "duration_min": 60},
        ],
    )
    events = list(build_calendar([plan]).events)
    assert len(events) == 1


def test_empty_plan_list_produces_no_events() -> None:
    plan = _plan(date(2026, 5, 18), [])
    calendar = build_calendar([plan])
    assert len(list(calendar.events)) == 0


def test_missing_session_type_defaults_to_training() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [{"date": "2026-05-18", "duration_min": 60}],
    )
    event = next(iter(build_calendar([plan]).events))
    assert event.name == "Training"


def test_invalid_duration_falls_back_to_default() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [{"date": "2026-05-18", "session_type": "Cycling", "duration_min": "bad"}],
    )
    event = next(iter(build_calendar([plan]).events))
    assert event.duration.total_seconds() == 60 * 60


def test_description_combines_description_intensity_and_notes() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [
            {
                "date": "2026-05-18",
                "session_type": "Cycling",
                "description": "Endurance ride",
                "intensity": "Easy",
                "notes": "Zone 2 only",
                "duration_min": 60,
            }
        ],
    )
    event = next(iter(build_calendar([plan]).events))
    assert "Endurance ride" in event.description
    assert "Intensity: Easy" in event.description
    assert "Notes: Zone 2 only" in event.description


def test_render_returns_valid_ics_string() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [{"date": "2026-05-18", "session_type": "Cycling", "duration_min": 60}],
    )
    output = render([plan])
    assert "BEGIN:VCALENDAR" in output
    assert "END:VCALENDAR" in output
    assert "BEGIN:VEVENT" in output
    assert "SUMMARY:Cycling" in output


def test_day_and_type_aliases_are_accepted() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [
            {
                "day": "2026-05-15",
                "type": "cycling",
                "title": "Rebuild Z2 Progressiv — 80 min",
                "time": "12:30",
                "description": "Progressive aerobic base.",
            }
        ],
    )
    events = list(build_calendar([plan]).events)
    assert len(events) == 1
    event = events[0]
    assert event.name == "Rebuild Z2 Progressiv — 80 min"
    assert event.begin.hour == 12
    assert event.begin.minute == 30
    assert "Progressive aerobic base." in event.description


def test_title_is_preferred_over_session_type_for_name() -> None:
    plan = _plan(
        date(2026, 5, 18),
        [
            {
                "date": "2026-05-18",
                "session_type": "Cycling",
                "title": "Endurance Ride 90 min",
                "duration_min": 90,
            }
        ],
    )
    event = next(iter(build_calendar([plan]).events))
    assert event.name == "Endurance Ride 90 min"


def test_invalid_plan_field_is_skipped() -> None:
    plan = WeeklyPlan(
        week_of=date(2026, 5, 18),
        version=1,
        plan="not-a-list",  # type: ignore[arg-type]
        notes="",
        is_current=True,
    )
    plan.id = uuid4()
    calendar = build_calendar([plan])
    assert len(list(calendar.events)) == 0
