from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from training_pipeline.notion_sync import activities_mirror
from training_pipeline.shared.models import Activity, ManualLog


def _render(stmt: Any) -> str:
    try:
        return str(
            stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
    except Exception:
        return str(stmt)


def _make_activity(
    *,
    notion_page_id: str | None = None,
    sport_type: str = "cycling",
    name: str = "morning ride",
) -> Activity:
    a = Activity(
        source="strava",
        source_id="123",
        start_time=datetime(2026, 5, 14, 7, 0, tzinfo=UTC),
        sport_type=sport_type,
        name=name,
        duration_seconds=3600,
        distance_meters=30000.0,
        avg_hr=145,
        training_load=80.0,
        raw={},
        ingested_at=datetime(2026, 5, 14, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 14, 8, 0, tzinfo=UTC),
    )
    a.id = uuid4()
    a.notion_page_id = notion_page_id
    return a


def _make_log(activity_id: UUID, rpe: int = 7) -> ManualLog:
    log = ManualLog(
        logged_at=datetime(2026, 5, 14, 18, 0, tzinfo=UTC),
        activity_id=activity_id,
        rpe=rpe,
        pain_score=0,
        notes="solid",
    )
    log.id = uuid4()
    return log


class _ScalarsResult:
    def __init__(self, items: Iterable[Any]) -> None:
        self._items = list(items)

    def __iter__(self) -> Any:
        return iter(self._items)


class FakeSession:
    def __init__(self, activities: list[Activity], logs: dict[UUID, ManualLog]) -> None:
        self.activities = activities
        self.logs = logs
        self.flush_calls = 0

    def scalars(self, stmt: Any) -> _ScalarsResult:
        return _ScalarsResult(self.activities)

    def scalar(self, stmt: Any) -> Any:
        rendered = _render(stmt)
        for aid, log in self.logs.items():
            if str(aid) in rendered:
                return log
        return None

    def flush(self) -> None:
        self.flush_calls += 1


def test_mirror_creates_new_activity_and_stores_page_id() -> None:
    activity = _make_activity()
    session = FakeSession([activity], {})
    client = MagicMock()
    client.create_page.return_value = {"id": "notion-page-1"}

    result = activities_mirror.mirror_activities(session, client, "db-acts")

    assert result == {"processed": 1, "created": 1, "updated": 0}
    assert activity.notion_page_id == "notion-page-1"
    client.create_page.assert_called_once()
    client.update_page.assert_not_called()
    assert session.flush_calls == 1


def test_mirror_updates_when_page_id_present() -> None:
    activity = _make_activity(notion_page_id="notion-page-existing")
    session = FakeSession([activity], {})
    client = MagicMock()

    result = activities_mirror.mirror_activities(session, client, "db-acts")

    assert result == {"processed": 1, "created": 0, "updated": 1}
    client.update_page.assert_called_once()
    page_id_arg = client.update_page.call_args.args[0]
    assert page_id_arg == "notion-page-existing"
    client.create_page.assert_not_called()


def test_mirror_includes_manual_log_fields() -> None:
    activity = _make_activity()
    log = _make_log(activity.id, rpe=8)
    session = FakeSession([activity], {activity.id: log})
    client = MagicMock()
    client.create_page.return_value = {"id": "p"}

    activities_mirror.mirror_activities(session, client, "db-acts")
    props = client.create_page.call_args.kwargs["properties"]
    assert props["RPE"] == {"number": 8}
    assert props["Pain"] == {"number": 0}
    assert props["Notes"]["rich_text"][0]["text"]["content"] == "solid"


def test_mirror_normalizes_sport_type() -> None:
    activity = _make_activity(sport_type="lifting")
    session = FakeSession([activity], {})
    client = MagicMock()
    client.create_page.return_value = {"id": "p"}

    activities_mirror.mirror_activities(session, client, "db-acts")
    props = client.create_page.call_args.kwargs["properties"]
    assert props["Sport"]["select"]["name"] == "Lifting"


def test_mirror_handles_unknown_sport_type() -> None:
    activity = _make_activity(sport_type="paddleboarding")
    session = FakeSession([activity], {})
    client = MagicMock()
    client.create_page.return_value = {"id": "p"}

    activities_mirror.mirror_activities(session, client, "db-acts")
    props = client.create_page.call_args.kwargs["properties"]
    assert props["Sport"]["select"]["name"] == "Other"


def test_mirror_handles_missing_optional_fields() -> None:
    activity = _make_activity()
    activity.duration_seconds = None
    activity.distance_meters = None
    activity.avg_hr = None
    activity.training_load = None
    session = FakeSession([activity], {})
    client = MagicMock()
    client.create_page.return_value = {"id": "p"}

    activities_mirror.mirror_activities(session, client, "db-acts")
    props = client.create_page.call_args.kwargs["properties"]
    assert "Duration (min)" not in props
    assert "Distance (km)" not in props
    assert "Avg HR" not in props
    assert "Training Load" not in props


def test_mirror_handles_no_activities() -> None:
    session = FakeSession([], {})
    client = MagicMock()

    result = activities_mirror.mirror_activities(session, client, "db-acts")
    assert result == {"processed": 0, "created": 0, "updated": 0}
    client.create_page.assert_not_called()
    client.update_page.assert_not_called()


def test_unnamed_activity_falls_back_to_synthetic_title() -> None:
    activity = _make_activity(name="")
    activity.name = None
    session = FakeSession([activity], {})
    client = MagicMock()
    client.create_page.return_value = {"id": "p"}

    activities_mirror.mirror_activities(session, client, "db-acts")
    props = client.create_page.call_args.kwargs["properties"]
    title = props["Name"]["title"][0]["text"]["content"]
    assert "cycling" in title.lower()


@pytest.mark.parametrize(
    "long_notes",
    ["x" * 5000],
)
def test_notes_are_truncated(long_notes: str) -> None:
    activity = _make_activity()
    log = _make_log(activity.id)
    log.notes = long_notes
    session = FakeSession([activity], {activity.id: log})
    client = MagicMock()
    client.create_page.return_value = {"id": "p"}

    activities_mirror.mirror_activities(session, client, "db-acts")
    props = client.create_page.call_args.kwargs["properties"]
    assert len(props["Notes"]["rich_text"][0]["text"]["content"]) == 2000
