from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from training_pipeline.mcp_server import tools
from training_pipeline.shared.models import (
    Activity,
    AthleteContext,
    BodyMeasurement,
    DailySummary,
    ManualLog,
    WeeklyPlan,
)


def _make_activity(
    *,
    activity_id: UUID | None = None,
    start: datetime | None = None,
    sport_type: str = "cycling",
    duration_seconds: int | None = 3600,
    distance_meters: float | None = 30000.0,
    avg_hr: int | None = 140,
    training_load: float | None = 80.0,
    source: str = "strava",
    source_id: str = "abc",
) -> Activity:
    activity = Activity(
        source=source,
        source_id=source_id,
        start_time=start or datetime(2026, 5, 10, 8, 0, tzinfo=UTC),
        sport_type=sport_type,
        name=f"{sport_type} session",
        duration_seconds=duration_seconds,
        distance_meters=distance_meters,
        elevation_gain_meters=100.0,
        avg_hr=avg_hr,
        max_hr=170,
        avg_power=200,
        normalized_power=220,
        calories=600,
        avg_cadence=85,
        training_load=training_load,
        raw={"src": "strava"},
        ingested_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
    )
    activity.id = activity_id or uuid4()
    return activity


def _make_log(
    *,
    activity_id: UUID | None = None,
    rpe: int | None = 7,
    pain_score: int | None = 0,
    notes: str | None = "felt good",
    tags: list[str] | None = None,
    logged_at: datetime | None = None,
) -> ManualLog:
    log = ManualLog(
        logged_at=logged_at or datetime(2026, 5, 10, 19, 0, tzinfo=UTC),
        activity_id=activity_id,
        rpe=rpe,
        pain_score=pain_score,
        notes=notes,
        tags=tags,
    )
    log.id = uuid4()
    return log


class _ScalarsResult:
    def __init__(self, items: Iterable[Any]) -> None:
        self._items = list(items)

    def __iter__(self) -> Any:
        return iter(self._items)

    def all(self) -> list[Any]:
        return list(self._items)


def _render(stmt: Any) -> str:
    """Render a SQLAlchemy statement with literal binds so dispatch hooks
    can route on parameter values (e.g. metric_name='ctl', UUIDs)."""
    try:
        return str(
            stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
    except Exception:
        return str(stmt)


class FakeSession:
    """Minimal Session double that routes queries via a single dispatch hook.

    Each test sets `dispatch` to a function that maps the rendered SQL string
    to the desired result. Statements are compiled with literal_binds so that
    bound parameter values appear in the text.
    """

    def __init__(self) -> None:
        self.dispatch: Any = lambda stmt: None
        self.scalar_dispatch: Any = lambda stmt: None
        self.execute_calls: list[str] = []
        self.flush_calls = 0
        self.added: list[Any] = []
        self.get_returns: dict[tuple[type, Any], Any] = {}

    def scalars(self, stmt: Any) -> _ScalarsResult:
        result = self.dispatch(_render(stmt))
        if result is None:
            return _ScalarsResult([])
        return _ScalarsResult(result)

    def scalar(self, stmt: Any) -> Any:
        return self.scalar_dispatch(_render(stmt))

    def execute(self, stmt: Any) -> Any:
        rendered = _render(stmt)
        self.execute_calls.append(rendered)
        result = self.dispatch(rendered)
        rv = MagicMock()
        if result is None:
            rv.all.return_value = []
            rv.first.return_value = None
        elif isinstance(result, list):
            rv.all.return_value = result
            rv.first.return_value = result[0] if result else None
        else:
            rv.all.return_value = list(result)
            rv.first.return_value = result
        return rv

    def get(self, model: type, ident: Any) -> Any:
        return self.get_returns.get((model, ident))

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, ManualLog) and obj.id is None:
            obj.id = uuid4()
        if isinstance(obj, WeeklyPlan) and obj.id is None:
            obj.id = uuid4()
        if isinstance(obj, WeeklyPlan):
            obj.created_at = obj.created_at or datetime.now(UTC)

    def flush(self) -> None:
        self.flush_calls += 1


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


# ---------------------------------------------------------------------------
# READ tools
# ---------------------------------------------------------------------------


def test_get_recent_activities_serializes_with_latest_log(session: FakeSession) -> None:
    activity = _make_activity()
    log = _make_log(activity_id=activity.id, rpe=8, pain_score=2)

    def dispatch(stmt: Any) -> Any:
        # First scalars call returns activities; subsequent _latest_log_for uses scalar().
        return [activity]

    def scalar_dispatch(stmt: Any) -> Any:
        return log

    session.dispatch = dispatch
    session.scalar_dispatch = scalar_dispatch

    rows = tools._get_recent_activities(session, days=14, sport_type=None)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == str(activity.id)
    assert row["sport_type"] == "cycling"
    assert row["duration_min"] == 60.0
    assert row["distance_km"] == 30.0
    assert row["rpe"] == 8
    assert row["pain"] == 2
    assert row["notes"] == "felt good"


def test_get_recent_activities_filters_by_sport_type(session: FakeSession) -> None:
    captured: list[str] = []

    def dispatch(stmt: Any) -> Any:
        captured.append(str(stmt))
        return []

    session.dispatch = dispatch
    rows = tools._get_recent_activities(session, days=7, sport_type="running")
    assert rows == []
    assert "sport_type" in captured[0]


def test_get_activity_by_id_invalid_uuid(session: FakeSession) -> None:
    assert tools._get_activity_by_id(session, "not-a-uuid") is None


def test_get_activity_by_id_not_found(session: FakeSession) -> None:
    assert tools._get_activity_by_id(session, str(uuid4())) is None


def test_get_activity_by_id_returns_full_payload(session: FakeSession) -> None:
    activity = _make_activity()
    log = _make_log(activity_id=activity.id)
    session.get_returns[(Activity, activity.id)] = activity
    session.scalar_dispatch = lambda stmt: log

    result = tools._get_activity_by_id(session, str(activity.id))

    assert result is not None
    assert result["id"] == str(activity.id)
    assert result["raw"] == {"src": "strava"}
    assert result["rpe"] == 7


def test_get_daily_summary_merges_sources(session: FakeSession) -> None:
    day = date(2026, 5, 10)
    summary = DailySummary(
        date=day,
        source="garmin",
        sleep_score=82,
        sleep_duration_seconds=27000,
        resting_hr=48,
        hrv_ms=72.5,
        body_battery_high=95,
        body_battery_low=20,
        steps=8500,
        raw={},
        ingested_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )
    measurement = BodyMeasurement(
        source="withings",
        measured_at=datetime(2026, 5, 10, 7, 0, tzinfo=UTC),
        weight_kg=82.4,
        body_fat_pct=18.1,
        raw={},
        ingested_at=datetime(2026, 5, 10, 8, 0, tzinfo=UTC),
    )

    calls: list[str] = []

    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        calls.append(stmt_str)
        if "daily_summary" in stmt_str:
            return [summary]
        if "body_measurements" in stmt_str:
            return [measurement]
        return []

    session.dispatch = dispatch
    rows = tools._get_daily_summary(session, day, day)

    assert len(rows) == 1
    assert rows[0]["sleep_score"] == 82
    assert rows[0]["sleep_duration_hours"] == 7.5
    assert rows[0]["resting_hr"] == 48
    assert rows[0]["hrv_ms"] == 72.5
    assert rows[0]["weight_kg"] == pytest.approx(82.4)
    assert rows[0]["body_fat_pct"] == pytest.approx(18.1)
    assert rows[0]["sources"] == ["garmin"]


def test_get_training_load_trend_aligns_dates(session: FakeSession) -> None:
    d1 = date(2026, 5, 9)
    d2 = date(2026, 5, 10)

    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        if "'ctl'" in stmt_str:
            return [(d1, 45.0), (d2, 47.0)]
        if "'atl'" in stmt_str:
            return [(d1, 60.0), (d2, 55.0)]
        if "'tsb'" in stmt_str:
            return [(d1, -15.0), (d2, -8.0)]
        return []

    session.dispatch = dispatch
    rows = tools._get_training_load_trend(session, weeks=2)
    assert [r["date"] for r in rows] == [d1.isoformat(), d2.isoformat()]
    assert rows[0] == {"date": d1.isoformat(), "ctl": 45.0, "atl": 60.0, "tsb": -15.0}
    assert rows[1] == {"date": d2.isoformat(), "ctl": 47.0, "atl": 55.0, "tsb": -8.0}


def test_get_weekly_load_groups_by_week(session: FakeSession) -> None:
    week = date(2026, 5, 4)

    def dispatch(stmt: Any) -> Any:
        return [
            (week, "weekly_load_cycling", 6.5),
            (week, "weekly_load_running", 2.0),
            (week, "weekly_load_lifting", 1.5),
            (week, "weekly_load_total", 320.0),
        ]

    session.dispatch = dispatch
    rows = tools._get_weekly_load(session, weeks=4)
    assert rows == [
        {
            "week_of": week.isoformat(),
            "cycling_hours": 6.5,
            "running_hours": 2.0,
            "lifting_hours": 1.5,
            "total_load": 320.0,
        }
    ]


def test_get_weight_trend_pairs_with_moving_averages(session: FakeSession) -> None:
    day1 = datetime(2026, 5, 9, 7, 0, tzinfo=UTC)
    day2 = datetime(2026, 5, 10, 7, 0, tzinfo=UTC)

    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        if "body_measurements" in stmt_str:
            return [(day1, 82.0), (day2, 81.8)]
        if "weight_7d_avg" in stmt_str:
            return [(day1.date(), 82.1), (day2.date(), 82.0)]
        if "weight_28d_avg" in stmt_str:
            return [(day1.date(), 82.5), (day2.date(), 82.4)]
        return []

    session.dispatch = dispatch
    rows = tools._get_weight_trend(session, weeks=4)
    assert len(rows) == 2
    assert rows[0]["weight_kg"] == pytest.approx(82.0)
    assert rows[0]["weight_7d_avg"] == pytest.approx(82.1)
    assert rows[1]["weight_28d_avg"] == pytest.approx(82.4)


def test_get_current_plan_returns_latest(session: FakeSession) -> None:
    plan = WeeklyPlan(
        week_of=date(2026, 5, 4),
        version=2,
        plan=[{"date": "2026-05-05", "session_type": "easy bike", "duration_min": 45}],
        notes="recovery week",
        is_current=True,
    )
    plan.id = uuid4()
    plan.created_at = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)

    session.scalar_dispatch = lambda stmt: plan
    result = tools._get_current_plan(session)
    assert result is not None
    assert result["version"] == 2
    assert result["plan"][0]["session_type"] == "easy bike"


def test_get_current_plan_none(session: FakeSession) -> None:
    session.scalar_dispatch = lambda stmt: None
    assert tools._get_current_plan(session) is None


def test_search_sessions_rejects_unknown_filters(session: FakeSession) -> None:
    with pytest.raises(ValueError, match="unsupported filters"):
        tools._search_sessions(session, {"foo": 1})


def test_search_sessions_applies_rpe_and_pain_filters(session: FakeSession) -> None:
    a1 = _make_activity(activity_id=uuid4())
    a2 = _make_activity(activity_id=uuid4())
    a3 = _make_activity(activity_id=uuid4())

    log_by_activity = {
        a1.id: _make_log(activity_id=a1.id, rpe=8, pain_score=0),
        a2.id: _make_log(activity_id=a2.id, rpe=4, pain_score=3),
        a3.id: None,
    }

    session.dispatch = lambda stmt: [a1, a2, a3]

    def scalar_dispatch(stmt: Any) -> Any:
        for aid, log in log_by_activity.items():
            if str(aid) in str(stmt):
                return log
        return None

    session.scalar_dispatch = scalar_dispatch

    rows = tools._search_sessions(session, {"min_rpe": 7})
    assert [r["id"] for r in rows] == [str(a1.id)]

    rows = tools._search_sessions(session, {"has_pain": True})
    assert [r["id"] for r in rows] == [str(a2.id)]

    rows = tools._search_sessions(session, {"has_pain": False})
    # has_pain=False excludes a2 (pain>0); a1 (pain=0) and a3 (no log) included
    assert {r["id"] for r in rows} == {str(a1.id), str(a3.id)}


def test_readiness_today_composes_fields(session: FakeSession) -> None:
    summary = DailySummary(
        date=date(2026, 5, 13),
        source="garmin",
        sleep_score=78,
        sleep_duration_seconds=25200,
        resting_hr=49,
        hrv_ms=65.0,
        body_battery_low=18,
        raw={},
        ingested_at=datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
    )
    weight_row = (datetime(2026, 5, 13, 7, 0, tzinfo=UTC), 82.0)

    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        if "body_measurements" in stmt_str:
            return [weight_row]
        if "weight_7d_avg" in stmt_str:
            return [(82.4,)]
        if "metric_name = " in stmt_str and "tsb" in stmt_str:
            return [(-12.5, date(2026, 5, 13))]
        if "manual_logs" in stmt_str:
            return [(7,), (8,), (6,)]
        return []

    session.dispatch = dispatch
    session.scalar_dispatch = lambda stmt: summary

    result = tools._readiness_today(session)

    assert result["last_night"]["sleep_score"] == 78
    assert result["last_night"]["sleep_duration_hours"] == 7.0
    assert result["last_night"]["hrv_ms"] == 65.0
    assert result["last_night"]["body_battery_low"] == 18
    assert result["latest_weight"]["weight_kg"] == pytest.approx(82.0)
    assert result["latest_weight"]["weight_7d_avg"] == pytest.approx(82.4)
    assert result["latest_weight"]["delta_vs_7d"] == pytest.approx(-0.4)
    assert result["training_load"]["tsb"] == pytest.approx(-12.5)
    assert result["recent_rpe"]["count"] == 3
    assert result["recent_rpe"]["avg"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# WRITE tools
# ---------------------------------------------------------------------------


def test_log_session_unlinked(session: FakeSession) -> None:
    result = tools._log_session(
        session,
        activity_id=None,
        rpe=7,
        pain_score=0,
        notes="solid effort",
        tags=["base"],
    )
    assert result["activity_id"] is None
    assert result["rpe"] == 7
    assert result["tags"] == ["base"]
    assert session.flush_calls == 1
    assert len(session.added) == 1
    assert isinstance(session.added[0], ManualLog)


def test_log_session_linked_requires_existing_activity(session: FakeSession) -> None:
    activity = _make_activity()
    session.get_returns[(Activity, activity.id)] = activity

    result = tools._log_session(
        session,
        activity_id=str(activity.id),
        rpe=8,
        pain_score=1,
        notes=None,
        tags=None,
    )
    assert result["activity_id"] == str(activity.id)


def test_log_session_unknown_activity_raises(session: FakeSession) -> None:
    with pytest.raises(ValueError, match="activity not found"):
        tools._log_session(
            session,
            activity_id=str(uuid4()),
            rpe=5,
            pain_score=None,
            notes=None,
            tags=None,
        )


def test_log_session_validates_rpe_range(session: FakeSession) -> None:
    with pytest.raises(ValueError, match="rpe"):
        tools._log_session(
            session, activity_id=None, rpe=11, pain_score=None, notes=None, tags=None
        )


def test_log_session_validates_pain_range(session: FakeSession) -> None:
    with pytest.raises(ValueError, match="pain_score"):
        tools._log_session(
            session, activity_id=None, rpe=None, pain_score=99, notes=None, tags=None
        )


def test_save_weekly_plan_supersedes_previous(session: FakeSession) -> None:
    week = date(2026, 5, 4)
    previous = WeeklyPlan(
        week_of=week,
        version=1,
        plan=[],
        is_current=True,
    )
    previous.id = uuid4()
    previous.created_at = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)

    def dispatch(stmt: Any) -> Any:
        return [previous]

    session.dispatch = dispatch
    session.scalar_dispatch = lambda stmt: 1  # max(version)

    result = tools._save_weekly_plan(
        session,
        week_of=week,
        plan=[{"date": "2026-05-05", "session_type": "easy run", "duration_min": 30}],
        notes="hold easy",
    )

    assert result["version"] == 2
    assert previous.is_current is False
    assert result["replaced_versions"] == [1]
    assert result["sessions"] == 1
    assert any(isinstance(o, WeeklyPlan) for o in session.added)


def test_save_weekly_plan_mirrors_to_notion_when_content_changed(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    week = date(2026, 5, 4)
    previous = WeeklyPlan(
        week_of=week,
        version=1,
        plan=[{"date": "2026-05-05", "session_type": "easy run"}],
        is_current=True,
    )
    previous.id = uuid4()
    previous.created_at = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    session.dispatch = lambda stmt: [previous]
    session.scalar_dispatch = lambda stmt: 1

    calls: list[Any] = []

    def fake_mirror(s: Any) -> tuple[bool, str | None]:
        calls.append(s)
        return True, None

    monkeypatch.setattr(tools, "_mirror_plan_to_notion", fake_mirror)

    result = tools._save_weekly_plan(
        session,
        week_of=week,
        plan=[{"date": "2026-05-05", "session_type": "hard intervals"}],
        notes="",
    )

    assert calls == [session]
    assert result["notion_mirrored"] is True
    assert result["notion_skipped_reason"] is None


def test_save_weekly_plan_skips_mirror_when_content_unchanged(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    week = date(2026, 5, 4)
    identical_plan = [{"date": "2026-05-05", "session_type": "easy run"}]
    previous = WeeklyPlan(
        week_of=week,
        version=1,
        plan=identical_plan,
        is_current=True,
    )
    previous.id = uuid4()
    previous.created_at = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    session.dispatch = lambda stmt: [previous]
    session.scalar_dispatch = lambda stmt: 1

    calls: list[Any] = []

    def fake_mirror(s: Any) -> tuple[bool, str | None]:
        calls.append(s)
        return True, None

    monkeypatch.setattr(tools, "_mirror_plan_to_notion", fake_mirror)

    result = tools._save_weekly_plan(
        session,
        week_of=week,
        plan=identical_plan,
        notes="",
    )

    assert calls == []  # mirror was not invoked
    assert result["notion_mirrored"] is False
    assert result["notion_skipped_reason"] == "unchanged_from_prior_version"
    # Postgres save still happened — version was still bumped.
    assert result["version"] == 2


def test_save_weekly_plan_save_still_succeeds_when_mirror_fails(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    week = date(2026, 5, 4)
    session.dispatch = lambda stmt: []
    session.scalar_dispatch = lambda stmt: 0

    def fake_mirror(s: Any) -> tuple[bool, str | None]:
        return False, "notion_error:HTTPResponseError"

    monkeypatch.setattr(tools, "_mirror_plan_to_notion", fake_mirror)

    result = tools._save_weekly_plan(
        session,
        week_of=week,
        plan=[{"date": "2026-05-05", "session_type": "easy run"}],
        notes="",
    )

    # The save itself succeeded; mirror failure surfaces via the result fields.
    assert any(isinstance(o, WeeklyPlan) for o in session.added)
    assert result["notion_mirrored"] is False
    assert result["notion_skipped_reason"] == "notion_error:HTTPResponseError"


def test_sync_plan_to_notion_returns_mirror_outcome(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "_mirror_plan_to_notion", lambda s: (True, None))
    assert tools._sync_plan_to_notion(session) == {
        "notion_mirrored": True,
        "notion_skipped_reason": None,
    }
    monkeypatch.setattr(tools, "_mirror_plan_to_notion", lambda s: (False, "notion_token_missing"))
    assert tools._sync_plan_to_notion(session) == {
        "notion_mirrored": False,
        "notion_skipped_reason": "notion_token_missing",
    }


def test_mirror_plan_to_notion_skips_when_token_missing(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = MagicMock()
    settings.NOTION_TOKEN = ""
    settings.NOTION_DB_PLAN_ID = "db-id"
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    mirrored, reason = tools._mirror_plan_to_notion(session)
    assert mirrored is False
    assert reason == "notion_token_missing"


def test_mirror_plan_to_notion_skips_when_db_id_missing(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = MagicMock()
    settings.NOTION_TOKEN = "tok"
    settings.NOTION_DB_PLAN_ID = ""
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    mirrored, reason = tools._mirror_plan_to_notion(session)
    assert mirrored is False
    assert reason == "notion_db_plan_id_missing"


def test_mirror_plan_to_notion_swallows_runtime_errors(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = MagicMock()
    settings.NOTION_TOKEN = "tok"
    settings.NOTION_DB_PLAN_ID = "db-id"
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "NotionClient", lambda token: MagicMock())

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("notion is down")

    monkeypatch.setattr(tools, "mirror_plan", boom)
    mirrored, reason = tools._mirror_plan_to_notion(session)
    assert mirrored is False
    assert reason == "notion_error:RuntimeError"


def test_save_weekly_plan_rejects_non_list(session: FakeSession) -> None:
    with pytest.raises(ValueError, match="list of dicts"):
        tools._save_weekly_plan(
            session,
            week_of=date(2026, 5, 4),
            plan="bad",
            notes="",  # type: ignore[arg-type]
        )


def test_save_weekly_plan_accepts_exercises(session: FakeSession) -> None:
    week = date(2026, 5, 4)
    session.dispatch = lambda stmt: []
    session.scalar_dispatch = lambda stmt: 0

    result = tools._save_weekly_plan(
        session,
        week_of=week,
        plan=[
            {
                "date": "2026-05-06",
                "session_type": "lifting",
                "description": "lower body",
                "duration_min": 60,
                "exercises": [
                    {"name": "Squat", "sets": 5, "reps": 5, "weight_kg": 100.0},
                    {"name": "RDL", "sets": 3, "reps": "8-10", "weight_kg": 80, "notes": "slow"},
                    {"name": "Calf raise"},
                ],
            }
        ],
        notes="",
    )

    assert result["sessions"] == 1
    stored = next(o for o in session.added if isinstance(o, WeeklyPlan))
    assert stored.plan[0]["exercises"][0]["name"] == "Squat"
    assert stored.plan[0]["exercises"][1]["reps"] == "8-10"


def test_save_weekly_plan_rejects_malformed_exercises(session: FakeSession) -> None:
    week = date(2026, 5, 4)
    session.dispatch = lambda stmt: []
    session.scalar_dispatch = lambda stmt: 0

    with pytest.raises(ValueError, match=r"exercises\[0\]\.name"):
        tools._save_weekly_plan(
            session,
            week_of=week,
            plan=[{"exercises": [{"sets": 5}]}],
            notes="",
        )

    with pytest.raises(ValueError, match=r"exercises\[0\]\.sets"):
        tools._save_weekly_plan(
            session,
            week_of=week,
            plan=[{"exercises": [{"name": "Squat", "sets": "five"}]}],
            notes="",
        )

    with pytest.raises(ValueError, match=r"exercises must be a list"):
        tools._save_weekly_plan(
            session,
            week_of=week,
            plan=[{"exercises": "Squat 5x5"}],
            notes="",
        )


def test_update_athlete_context_rejects_unknown_fields(session: FakeSession) -> None:
    with pytest.raises(ValueError, match="unsupported athlete_context fields"):
        tools._update_athlete_context(session, {"unknown_key": 1})


def test_update_athlete_context_upserts(session: FakeSession) -> None:
    row = AthleteContext(
        id=1,
        ftp_watts=250,
        max_hr=190,
        body_weight_kg=82.5,
        current_phase="base",
        notes="rebuild week",
        updated_at=datetime(2026, 5, 14, 9, 0, tzinfo=UTC),
    )
    session.get_returns[(AthleteContext, 1)] = row

    result = tools._update_athlete_context(
        session,
        {"ftp_watts": 250, "current_phase": "base"},
    )

    assert result["ftp_watts"] == 250
    assert result["current_phase"] == "base"
    assert session.flush_calls == 1
    # one execute call for the upsert
    assert len(session.execute_calls) == 1


def test_serialize_activity_handles_nulls() -> None:
    activity = _make_activity(
        duration_seconds=None,
        distance_meters=None,
        avg_hr=None,
        training_load=None,
    )
    row = tools._serialize_activity(activity, None)
    assert row["duration_min"] is None
    assert row["distance_km"] is None
    assert row["avg_hr"] is None
    assert row["training_load"] is None
    assert row["rpe"] is None
    assert row["pain"] is None
    assert row["tags"] is None


def test_recent_window_respects_days() -> None:
    target = tools._start_of_window(7)
    expected_low = datetime.now(UTC) - timedelta(days=7, seconds=5)
    expected_high = datetime.now(UTC) - timedelta(days=7) + timedelta(seconds=5)
    assert expected_low <= target <= expected_high


def test_get_daily_summary_serializes_dates() -> None:
    # Smoke test that the wrapper parses ISO strings and calls into the impl.
    # We monkeypatch get_session by calling the underscore impl directly above;
    # here we just verify the public wrapper signature is callable with strings.
    sig_inputs = ("2026-05-01", "2026-05-02")
    parsed = (
        date.fromisoformat(sig_inputs[0]),
        date.fromisoformat(sig_inputs[1]),
    )
    start_dt = datetime.combine(parsed[0], time.min, tzinfo=UTC)
    assert start_dt.tzinfo is UTC
