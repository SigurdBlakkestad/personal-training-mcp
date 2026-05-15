"""Unit tests for the calendar publisher."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from training_pipeline.calendar_publish import publisher
from training_pipeline.calendar_publish.publisher import (
    fetch_plans,
    write_calendar,
)
from training_pipeline.shared.models import WeeklyPlan


def _plan(week_of: date, sessions: list[dict[str, Any]], is_current: bool = True) -> WeeklyPlan:
    plan = WeeklyPlan(
        week_of=week_of,
        version=1,
        plan=sessions,
        notes="",
        is_current=is_current,
    )
    plan.id = uuid4()
    return plan


def test_fetch_plans_returns_current_and_future_only() -> None:
    today = date(2026, 5, 13)  # Wednesday
    expected_monday = date(2026, 5, 11)

    matching_plan = _plan(expected_monday, [])
    future_plan = _plan(date(2026, 5, 18), [])

    captured: dict[str, Any] = {}

    def fake_scalars(stmt: Any) -> Any:
        captured["stmt"] = stmt
        result = MagicMock()
        result.__iter__.return_value = iter([matching_plan, future_plan])
        return result

    session = MagicMock()
    session.scalars.side_effect = fake_scalars

    plans = fetch_plans(session, today=today, weeks_ahead=4)
    assert plans == [matching_plan, future_plan]
    compiled = str(captured["stmt"])
    assert "weekly_plans" in compiled
    assert "is_current" in compiled


def test_write_calendar_creates_file_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "subdir" / "training.ics"
    plan = _plan(
        date(2026, 5, 18),
        [{"date": "2026-05-18", "session_type": "Cycling", "duration_min": 60}],
    )
    changed = write_calendar([plan], target)
    assert changed is True
    assert target.exists()
    assert b"BEGIN:VCALENDAR" in target.read_bytes()


def test_write_calendar_returns_false_when_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "training.ics"
    plan = _plan(
        date(2026, 5, 18),
        [{"date": "2026-05-18", "session_type": "Cycling", "duration_min": 60}],
    )
    first = write_calendar([plan], target)
    second = write_calendar([plan], target)
    assert first is True
    assert second is False


def test_write_calendar_returns_true_when_content_changes(tmp_path: Path) -> None:
    target = tmp_path / "training.ics"
    plan_v1 = _plan(
        date(2026, 5, 18),
        [{"date": "2026-05-18", "session_type": "Cycling", "duration_min": 60}],
    )
    plan_v2 = _plan(
        date(2026, 5, 18),
        [{"date": "2026-05-18", "session_type": "Lifting", "duration_min": 60}],
    )
    assert write_calendar([plan_v1], target) is True
    assert write_calendar([plan_v2], target) is True


def test_publish_returns_counts_and_change_flag(tmp_path: Path, monkeypatch: Any) -> None:
    plan = _plan(
        date(2026, 5, 18),
        [
            {"date": "2026-05-18", "session_type": "Cycling", "duration_min": 60},
            {"date": "2026-05-20", "session_type": "Lifting", "duration_min": 45},
        ],
    )

    class _SessionCM:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(publisher, "get_session", lambda: _SessionCM())
    monkeypatch.setattr(publisher, "fetch_plans", lambda *_a, **_kw: [plan])

    output_path = tmp_path / "training.ics"
    result = publisher.publish(output_path=output_path)

    assert result.changed is True
    assert result.plans == 1
    assert result.sessions == 2
    assert result.output_path == output_path
    assert output_path.exists()


def test_publish_handles_no_plans(tmp_path: Path, monkeypatch: Any) -> None:
    class _SessionCM:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(publisher, "get_session", lambda: _SessionCM())
    monkeypatch.setattr(publisher, "fetch_plans", lambda *_a, **_kw: [])

    output_path = tmp_path / "training.ics"
    result = publisher.publish(output_path=output_path)

    assert result.plans == 0
    assert result.sessions == 0
    assert result.changed is True
    assert b"BEGIN:VCALENDAR" in output_path.read_bytes()
