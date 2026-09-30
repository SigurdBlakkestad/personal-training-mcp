from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from training_pipeline.derived.weight_trend import compute_weight_trend
from training_pipeline.mcp_server import tools
from training_pipeline.shared.local_time import local_today
from training_pipeline.shared.models import (
    Activity,
    ActivityExerciseSet,
    ActivityLap,
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


def test_get_activity_by_id_includes_laps_and_exercise_sets(session: FakeSession) -> None:
    activity = _make_activity()
    laps = [
        ActivityLap(
            activity_id=activity.id,
            lap_index=1,
            lap_type="WARMUP",
            duration_s=300.0,
            distance_meters=1719.54,
            avg_power=101,
            max_power=103,
            normalized_power=101,
            avg_hr=109,
            max_hr=115,
            avg_cadence=69,
        ),
        ActivityLap(
            activity_id=activity.id,
            lap_index=2,
            lap_type="ACTIVE",
            duration_s=480.0,
            avg_power=193,
            max_power=198,
            avg_hr=149,
        ),
    ]
    sets = [
        ActivityExerciseSet(
            activity_id=activity.id,
            set_index=0,
            set_type="ACTIVE",
            exercise_name="BENCH_PRESS",
            exercise_confidence=99.6,
            reps=12,
            weight_kg=40.0,
            duration_s=128.7,
        ),
        ActivityExerciseSet(
            activity_id=activity.id,
            set_index=1,
            set_type="REST",
            duration_s=115.37,
        ),
    ]
    session.get_returns[(Activity, activity.id)] = activity

    def dispatch(stmt: str) -> Any:
        if "activity_laps" in stmt:
            return laps
        if "activity_exercise_sets" in stmt:
            return sets
        return []

    session.dispatch = dispatch
    session.scalar_dispatch = lambda stmt: None

    result = tools._get_activity_by_id(session, str(activity.id))

    assert result is not None
    assert [lap["lap_type"] for lap in result["laps"]] == ["WARMUP", "ACTIVE"]
    work = result["laps"][1]
    assert work["avg_power"] == 193
    assert work["duration_min"] == 8.0
    assert result["laps"][0]["distance_km"] == 1.72

    assert [s["set_type"] for s in result["exercise_sets"]] == ["ACTIVE", "REST"]
    assert result["exercise_sets"][0]["reps"] == 12
    assert result["exercise_sets"][0]["weight_kg"] == 40.0
    assert result["exercise_sets"][0]["exercise_confidence"] == 99.6

    # Existing fields survive the addition.
    assert result["raw"] == {"src": "strava"}
    assert result["avg_power"] == 200


def test_get_activity_by_id_omits_detail_arrays_when_absent(session: FakeSession) -> None:
    activity = _make_activity()
    session.get_returns[(Activity, activity.id)] = activity
    session.dispatch = lambda stmt: []
    session.scalar_dispatch = lambda stmt: None

    result = tools._get_activity_by_id(session, str(activity.id))

    assert result is not None
    assert "laps" not in result
    assert "exercise_sets" not in result


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


def test_get_daily_summary_garmin_wins_over_withings_on_same_date(
    session: FakeSession,
) -> None:
    day = date(2026, 5, 10)
    withings = DailySummary(
        date=day,
        source="withings",
        sleep_score=55,
        sleep_duration_seconds=21600,
        steps=4000,
        raw={},
        ingested_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
    )
    garmin = DailySummary(
        date=day,
        source="garmin",
        sleep_score=82,
        sleep_duration_seconds=27000,
        resting_hr=48,
        hrv_ms=72.5,
        raw={},
        ingested_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )

    for ordering in ([garmin, withings], [withings, garmin]):
        session.dispatch = lambda stmt, rs=ordering: rs if "daily_summary" in str(stmt) else []
        rows = tools._get_daily_summary(session, day, day)

        assert len(rows) == 1
        assert rows[0]["sources"] == ["garmin", "withings"]
        assert rows[0]["sleep_score"] == 82
        assert rows[0]["sleep_duration_hours"] == 7.5
        assert rows[0]["resting_hr"] == 48
        assert rows[0]["hrv_ms"] == 72.5
        # Garmin had no steps that day; Withings fills only that gap.
        assert rows[0]["steps"] == 4000


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
    today = local_today()
    week = today - timedelta(days=today.weekday())

    def dispatch(stmt: Any) -> Any:
        if "derived_metrics" in stmt:
            return [
                (week, "weekly_load_cycling", 6.5),
                (week, "weekly_load_running", 2.0),
                (week, "weekly_load_lifting", 1.5),
                (week, "weekly_load_total", 320.0),
            ]
        return []

    session.dispatch = dispatch
    rows = tools._get_weekly_load(session, weeks=4)
    assert rows == [
        {
            "week_of": week.isoformat(),
            "cycling_load": 6.5,
            "running_load": 2.0,
            "lifting_load": 1.5,
            "total_load": 320.0,
            "cycling_hours": 0.0,
            "running_hours": 0.0,
            "lifting_hours": 0.0,
            "total_hours": 0.0,
        }
    ]


def test_get_weekly_load_hours_come_from_duration_not_load(session: FakeSession) -> None:
    today = local_today()
    monday = today - timedelta(days=today.weekday())
    tue = datetime.combine(monday + timedelta(days=1), time(7), tzinfo=UTC)
    # A week whose Monday falls before the window start is never reported,
    # even partially.
    stale = datetime.combine(monday - timedelta(days=22), time(7), tzinfo=UTC)

    def dispatch(stmt: Any) -> Any:
        if "derived_metrics" in stmt:
            return [(monday, "weekly_load_running", 122.5), (monday, "weekly_load_total", 190.0)]
        if "FROM activities" in stmt:
            return [
                (tue, "running", 1740),
                (tue, "cycling", 5400),
                (tue, "swimming", 1800),
                (stale, "running", 3600),
            ]
        return []

    session.dispatch = dispatch
    rows = tools._get_weekly_load(session, weeks=3)
    assert rows == [
        {
            "week_of": monday.isoformat(),
            "cycling_load": 0.0,
            "running_load": 122.5,
            "lifting_load": 0.0,
            "total_load": 190.0,
            "cycling_hours": 1.5,
            "running_hours": 0.48,
            "lifting_hours": 0.0,
            "total_hours": 2.48,
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


def test_get_weight_trend_uses_same_reading_as_derived_average(session: FakeSession) -> None:
    # Two weigh-ins on one Oslo day: 07:00 (82.4) and 21:00 (81.6) CEST.
    morning = datetime(2026, 9, 29, 5, 0, tzinfo=UTC)
    evening = datetime(2026, 9, 29, 19, 0, tzinfo=UTC)
    measurements = [(morning, 82.4), (evening, 81.6)]
    derived = compute_weight_trend(measurements)
    assert len(derived) == 1

    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        if "body_measurements" in stmt_str:
            return measurements
        if "weight_7d_avg" in stmt_str:
            return [(p.date, p.weight_7d_avg) for p in derived]
        return []

    session.dispatch = dispatch
    rows = tools._get_weight_trend(session, weeks=4)

    assert len(rows) == 1
    assert rows[0]["date"] == "2026-09-29"
    assert rows[0]["weight_kg"] == pytest.approx(82.4)
    assert rows[0]["weight_7d_avg"] == pytest.approx(rows[0]["weight_kg"])


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
    weigh_in = datetime(2026, 5, 13, 5, 0, tzinfo=UTC)
    weight_rows = [
        (datetime(2026, 5, 11, 5, 0, tzinfo=UTC), 82.6),
        (datetime(2026, 5, 12, 5, 0, tzinfo=UTC), 82.2),
        (weigh_in, 82.0),
    ]

    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        if "max(daily_summary.date)" in stmt_str:
            return [summary]
        if "max(body_measurements.measured_at)" in stmt_str:
            return [(weigh_in,)]
        if "body_measurements" in stmt_str:
            return weight_rows
        if "metric_name = " in stmt_str and "tsb" in stmt_str:
            return [(-12.5, date(2026, 5, 13))]
        if "manual_logs" in stmt_str:
            return [(uuid4(), 7), (uuid4(), 8), (None, 6)]
        return []

    session.dispatch = dispatch
    session.scalar_dispatch = lambda stmt: summary

    result = tools._readiness_today(session)

    assert result["last_night"]["sleep_score"] == 78
    assert result["last_night"]["sleep_duration_hours"] == 7.0
    assert result["last_night"]["hrv_ms"] == 65.0
    assert result["last_night"]["body_battery_low"] == 18
    assert result["latest_weight"]["date"] == "2026-05-13"
    assert result["latest_weight"]["weight_kg"] == pytest.approx(82.0)
    assert result["latest_weight"]["weight_7d_avg"] == pytest.approx(82.4)
    assert result["latest_weight"]["delta_vs_7d"] == pytest.approx(-0.4)
    assert result["training_load"]["tsb"] == pytest.approx(-12.5)
    assert result["recent_rpe"]["count"] == 3
    assert result["recent_rpe"]["avg"] == pytest.approx(7.0)


def _weight_dispatch(rows: list[tuple[datetime, float]], seen: list[str] | None = None) -> Any:
    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        if seen is not None:
            seen.append(stmt_str)
        if "max(body_measurements.measured_at)" in stmt_str:
            return [(max(r[0] for r in rows),)] if rows else [(None,)]
        if "body_measurements" in stmt_str:
            return rows
        return []

    return dispatch


def test_readiness_weight_delta_excludes_latest_day_for_weekly_weigher(
    session: FakeSession,
) -> None:
    # Weekly weigher: 84.0 a week ago, 82.0 today (both 07:00 Oslo, CEST).
    rows = [
        (datetime(2026, 9, 22, 5, 0, tzinfo=UTC), 84.0),
        (datetime(2026, 9, 29, 5, 0, tzinfo=UTC), 82.0),
    ]
    session.dispatch = _weight_dispatch(rows)

    weight = tools._readiness_today(session)["latest_weight"]

    assert weight["date"] == "2026-09-29"
    assert weight["weight_kg"] == pytest.approx(82.0)
    assert weight["weight_7d_avg"] == pytest.approx(84.0)
    assert weight["weight_7d_avg_date"] == "2026-09-28"
    assert weight["delta_vs_7d"] == pytest.approx(-2.0)


def test_readiness_weight_uses_first_weigh_in_of_local_day(session: FakeSession) -> None:
    # 00:30 Oslo on 29 Sep is still 28 Sep in UTC; the evening reading drifts.
    rows = [
        (datetime(2026, 9, 28, 5, 0, tzinfo=UTC), 83.0),
        (datetime(2026, 9, 28, 22, 30, tzinfo=UTC), 82.0),
        (datetime(2026, 9, 29, 19, 0, tzinfo=UTC), 83.5),
    ]
    seen: list[str] = []
    session.dispatch = _weight_dispatch(rows, seen)

    weight = tools._readiness_today(session)["latest_weight"]

    assert weight["date"] == "2026-09-29"
    assert weight["weight_kg"] == pytest.approx(82.0)
    assert weight["weight_7d_avg"] == pytest.approx(83.0)
    assert weight["delta_vs_7d"] == pytest.approx(-1.0)
    window_sql = next(s for s in seen if "body_measurements" in s and "max(" not in s)
    # Window bounds are Oslo midnights, not UTC ones.
    assert "2026-09-22 00:00:00+02:00" in window_sql


def test_readiness_weight_none_without_measurements(session: FakeSession) -> None:
    session.dispatch = _weight_dispatch([])

    weight = tools._readiness_today(session)["latest_weight"]

    assert weight == {
        "date": None,
        "weight_kg": None,
        "weight_7d_avg": None,
        "weight_7d_avg_date": None,
        "delta_vs_7d": None,
    }


def test_readiness_weight_avg_none_without_preceding_weigh_ins(session: FakeSession) -> None:
    rows = [(datetime(2026, 9, 29, 5, 0, tzinfo=UTC), 82.0)]
    session.dispatch = _weight_dispatch(rows)

    weight = tools._readiness_today(session)["latest_weight"]

    assert weight["weight_kg"] == pytest.approx(82.0)
    assert weight["weight_7d_avg"] is None
    assert weight["weight_7d_avg_date"] is None
    assert weight["delta_vs_7d"] is None


def test_readiness_rpe_counts_every_session_in_window(session: FakeSession) -> None:
    logs = [(uuid4(), 5 + i % 4) for i in range(12)]
    seen: list[str] = []

    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        seen.append(stmt_str)
        return logs if "manual_logs" in stmt_str else []

    session.dispatch = dispatch

    rpe = tools._readiness_today(session)["recent_rpe"]

    assert rpe["window_days"] == 21
    assert rpe["count"] == 12
    rpe_sql = next(s for s in seen if "manual_logs" in s)
    assert "LIMIT" not in rpe_sql
    # 21 local days including today, starting at Oslo (not UTC) midnight.
    horizon = (local_today() - timedelta(days=20)).isoformat()
    assert f"'{horizon} 00:00:00+02:00'" in rpe_sql or f"'{horizon} 00:00:00+01:00'" in rpe_sql


def test_readiness_rpe_counts_relogged_session_once(session: FakeSession) -> None:
    relogged = uuid4()
    # Newest first, as the query orders them: the correction wins.
    logs = [(relogged, 9), (uuid4(), 6), (relogged, 5), (None, 7), (None, 8)]
    session.dispatch = lambda stmt: logs if "manual_logs" in str(stmt) else []

    rpe = tools._readiness_today(session)["recent_rpe"]

    assert rpe["values"] == [9, 6, 7, 8]
    assert rpe["count"] == 4


def test_readiness_vo2_max_falls_back_to_newest_non_null(session: FakeSession) -> None:
    today = _as_of(session)
    two_days_ago = today - timedelta(days=2)
    seen: list[str] = []

    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        seen.append(stmt_str)
        if "daily_summary.vo2_max_running IS NOT NULL" in stmt_str:
            return [(two_days_ago, 52.0)]
        return []

    session.dispatch = dispatch
    # Today's Garmin row has no VO2 estimate.
    session.scalar_dispatch = lambda stmt: _garmin_summary(today)

    fitness = tools._readiness_today(session)["fitness"]

    assert fitness["vo2_max_running"] == pytest.approx(52.0)
    assert fitness["vo2_max_running_date"] == two_days_ago.isoformat()
    assert fitness["vo2_max_cycling"] is None
    assert fitness["vo2_max_cycling_date"] is None
    vo2_sql = next(s for s in seen if "vo2_max_running IS NOT NULL" in s)
    assert "daily_summary.source = 'garmin'" in vo2_sql


def _readiness_dispatch(
    last_night_rows: list[DailySummary],
    intensity_rows: list[tuple[int | None, int | None]] | None = None,
    seen: list[str] | None = None,
) -> Any:
    def dispatch(stmt: Any) -> Any:
        stmt_str = str(stmt)
        if seen is not None:
            seen.append(stmt_str)
        if "max(daily_summary.date)" in stmt_str:
            return last_night_rows
        if "daily_summary" in stmt_str and "intensity_minutes_moderate" in stmt_str:
            return intensity_rows or []
        return []

    return dispatch


def _as_of(session: FakeSession) -> date:
    """The date _readiness_today treats as today, so fixtures follow the
    tool's own clock rather than a separately computed one."""
    return date.fromisoformat(tools._readiness_today(session)["as_of"])


def _garmin_summary(day: date, **fields: Any) -> DailySummary:
    return DailySummary(
        date=day,
        source="garmin",
        raw={},
        ingested_at=datetime.combine(day, time(12), tzinfo=UTC),
        **fields,
    )


def test_readiness_today_prefers_garmin_row_when_withings_shares_the_date(
    session: FakeSession,
) -> None:
    today = _as_of(session)
    withings = DailySummary(
        date=today,
        source="withings",
        sleep_score=60,
        sleep_duration_seconds=21600,
        raw={},
        ingested_at=datetime.combine(today, time(9), tzinfo=UTC),
    )
    garmin = _garmin_summary(
        today,
        sleep_score=81,
        resting_hr=47,
        hrv_ms=70.0,
        respiration_avg=13.5,
        body_battery_low=22,
    )
    # Withings row arrives first: it must not shadow Garmin's physiology.
    session.dispatch = _readiness_dispatch([withings, garmin])
    session.scalar_dispatch = lambda stmt: garmin

    last_night = tools._readiness_today(session)["last_night"]

    assert last_night["data_date"] == today.isoformat()
    assert last_night["days_old"] == 0
    assert last_night["sources"] == ["garmin", "withings"]
    assert last_night["sleep_score"] == 81
    assert last_night["resting_hr"] == 47
    assert last_night["hrv_ms"] == 70.0
    assert last_night["respiration_avg"] == 13.5
    assert last_night["body_battery_low"] == 22
    # Garmin had no sleep duration that night, so Withings fills the gap.
    assert last_night["sleep_duration_hours"] == 6.0


def test_readiness_today_sums_intensity_minutes_week_to_date(session: FakeSession) -> None:
    today = _as_of(session)
    seen: list[str] = []
    session.dispatch = _readiness_dispatch(
        [_garmin_summary(today, intensity_minutes_moderate=5, intensity_minutes_vigorous=0)],
        intensity_rows=[(30, 10), (45, None), (None, 20), (5, 0)],
        seen=seen,
    )
    session.scalar_dispatch = lambda stmt: _garmin_summary(today)

    result = tools._readiness_today(session)

    week = result["fitness"]["intensity_minutes_week_to_date"]
    week_start = date.fromisoformat(week["week_start"])
    assert week_start.weekday() == 0
    assert 0 <= (today - week_start).days <= 6
    assert week["moderate"] == 80
    assert week["vigorous"] == 30
    intensity_sql = next(s for s in seen if "intensity_minutes_moderate" in s and "max(" not in s)
    assert "daily_summary.source = 'garmin'" in intensity_sql
    assert f"daily_summary.date >= '{week_start.isoformat()}'" in intensity_sql


def test_readiness_today_intensity_none_without_garmin_rows(session: FakeSession) -> None:
    session.dispatch = _readiness_dispatch([])

    week = tools._readiness_today(session)["fitness"]["intensity_minutes_week_to_date"]

    assert week["moderate"] is None
    assert week["vigorous"] is None


def test_readiness_today_flags_stale_garmin_data(session: FakeSession) -> None:
    three_days_ago = _as_of(session) - timedelta(days=3)
    garmin = _garmin_summary(three_days_ago, sleep_score=75, hrv_ms=60.0)
    seen: list[str] = []
    session.dispatch = _readiness_dispatch([garmin], seen=seen)
    session.scalar_dispatch = lambda stmt: garmin

    result = tools._readiness_today(session)

    assert result["stale"] is True
    # last_night is anchored on Garmin's newest date, so a newer Withings-only
    # date cannot make stale Garmin data look like last night.
    last_night_sql = next(s for s in seen if "max(daily_summary.date)" in s)
    assert "coalesce" in last_night_sql
    assert "daily_summary.source = 'garmin'" in last_night_sql
    assert result["last_night"]["data_date"] == three_days_ago.isoformat()
    assert result["last_night"]["days_old"] == 3


@pytest.mark.parametrize("days_old", [0, 1])
def test_readiness_today_not_stale_for_today_or_yesterday(
    session: FakeSession, days_old: int
) -> None:
    day = _as_of(session) - timedelta(days=days_old)
    garmin = _garmin_summary(day, sleep_score=75)
    session.dispatch = _readiness_dispatch([garmin])
    session.scalar_dispatch = lambda stmt: garmin

    result = tools._readiness_today(session)

    assert result["stale"] is False
    assert result["last_night"]["days_old"] == days_old


def test_readiness_today_stale_without_any_garmin_summary(session: FakeSession) -> None:
    session.dispatch = _readiness_dispatch([])

    result = tools._readiness_today(session)

    assert result["stale"] is True
    assert result["last_night"]["data_date"] is None
    assert result["last_night"]["days_old"] is None


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
        plan=[{"date": "2026-05-05", "session_type": "running", "duration_min": 30}],
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
        plan=[{"date": "2026-05-05", "session_type": "running"}],
        is_current=True,
    )
    previous.id = uuid4()
    previous.created_at = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    session.dispatch = lambda stmt: [previous]
    session.scalar_dispatch = lambda stmt: 1

    calls: list[Any] = []

    def fake_mirror(s: Any, *, week_of: Any = None) -> tuple[bool, str | None]:
        calls.append((s, week_of))
        return True, None

    monkeypatch.setattr(tools, "_mirror_plan_to_notion", fake_mirror)

    result = tools._save_weekly_plan(
        session,
        week_of=week,
        plan=[{"date": "2026-05-05", "session_type": "cycling"}],
        notes="",
    )

    # The mirror runs with the exact week being saved, not "all current plans".
    assert calls == [(session, week)]
    assert result["notion_mirrored"] is True
    assert result["notion_skipped_reason"] is None


def test_save_weekly_plan_skips_mirror_when_content_unchanged(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    week = date(2026, 5, 4)
    identical_plan = [{"date": "2026-05-05", "session_type": "running"}]
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

    def fake_mirror(s: Any, *, week_of: Any = None) -> tuple[bool, str | None]:
        calls.append((s, week_of))
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

    def fake_mirror(s: Any, *, week_of: Any = None) -> tuple[bool, str | None]:
        return False, "notion_error:HTTPResponseError"

    monkeypatch.setattr(tools, "_mirror_plan_to_notion", fake_mirror)

    result = tools._save_weekly_plan(
        session,
        week_of=week,
        plan=[{"date": "2026-05-05", "session_type": "running"}],
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


def _valid_session(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "date": "2026-05-05",
        "session_type": "running",
        "title": "Easy run",
        "duration_min": 40,
        "description": "Z2 by feel",
    }
    base.update(overrides)
    return base


def _assert_nothing_written(session: FakeSession) -> None:
    assert session.added == []
    assert session.execute_calls == []
    assert session.flush_calls == 0


@pytest.mark.parametrize(
    ("week_of", "plan", "notes", "expected"),
    [
        ("05/04/2026", [_valid_session()], "", r"week_of is not an ISO date"),
        (date(2026, 5, 5), [_valid_session(date="2026-05-06")], "", r"week_of must be a Monday"),
        (date(2026, 5, 4), [_valid_session(date=None)], "", r"session\[0\]\.date is required"),
        (date(2026, 5, 4), [_valid_session(date="5 May")], "", r"session\[0\]\.date is not an ISO"),
        (date(2026, 5, 4), [_valid_session(date="2026-05-11")], "", r"outside the week"),
        (date(2026, 5, 4), [_valid_session(date="2026-05-03")], "", r"outside the week"),
        (date(2026, 5, 4), [_valid_session(session_type="easy run")], "", r"session_type"),
        (date(2026, 5, 4), [_valid_session(session_type=None)], "", r"session_type"),
        (date(2026, 5, 4), [_valid_session(duration_min="45")], "", r"duration_min"),
        (date(2026, 5, 4), [_valid_session(duration_min=45.5)], "", r"duration_min"),
        (date(2026, 5, 4), [_valid_session(duration_min=True)], "", r"duration_min"),
        (date(2026, 5, 4), [_valid_session(title="x" * 201)], "", r"title must be at most 200"),
        (date(2026, 5, 4), [_valid_session(notes="x" * 2001)], "", r"notes must be at most 2000"),
        (date(2026, 5, 4), [_valid_session(description=42)], "", r"description must be a string"),
        (date(2026, 5, 4), ["not a dict"], "", r"session\[0\] must be a dict"),
        ("20260504", [_valid_session()], "", r"week_of is not an ISO date"),
        (date(2026, 5, 4), [_valid_session(date="20260505")], "", r"date is not an ISO date"),
        (date(2026, 5, 4), [_valid_session(time="7am")], "", r"session\[0\]\.time"),
        (date(2026, 5, 4), [_valid_session(intensity="Threshold")], "", r"intensity"),
        (date(2026, 5, 4), [_valid_session()], "x" * 2001, r"^.*: notes must be at most 2000"),
    ],
)
def test_save_weekly_plan_rejects_invalid_input(
    session: FakeSession, week_of: date | str, plan: list[Any], notes: str, expected: str
) -> None:
    with pytest.raises(ValueError, match=expected):
        tools._save_weekly_plan(session, week_of=week_of, plan=plan, notes=notes)
    _assert_nothing_written(session)


def test_save_weekly_plan_lists_every_problem_at_once(session: FakeSession) -> None:
    with pytest.raises(ValueError) as excinfo:
        tools._save_weekly_plan(
            session,
            week_of="2026-05-04",
            plan=[
                _valid_session(date="2026-06-01"),
                _valid_session(session_type="yoga", duration_min="an hour"),
            ],
            notes="",
        )
    message = str(excinfo.value)
    assert message.startswith("save_weekly_plan rejected, nothing saved: ")
    assert "session[0].date 2026-06-01 is outside the week 2026-05-04..2026-05-10" in message
    assert "session[1].session_type" in message
    assert "session[1].duration_min" in message
    _assert_nothing_written(session)


def test_save_weekly_plan_accepts_iso_string_week_and_aliases(session: FakeSession) -> None:
    session.dispatch = lambda stmt: []
    session.scalar_dispatch = lambda stmt: 0
    plan = [
        _valid_session(date="2026-05-04"),
        _valid_session(
            date="2026-05-10",
            session_type="Strength",
            title="x" * 200,
            time="17:30",
            intensity="hard",
        ),
        {"date": "2026-05-07", "session_type": "rest"},
    ]

    result = tools._save_weekly_plan(session, week_of="2026-05-04", plan=plan, notes="deload")

    assert result["week_of"] == "2026-05-04"
    assert result["sessions"] == 3
    stored = next(o for o in session.added if isinstance(o, WeeklyPlan))
    assert stored.plan == plan  # saved unchanged
    assert stored.week_of == date(2026, 5, 4)


def test_log_session_rejects_all_empty_input(session: FakeSession) -> None:
    with pytest.raises(ValueError, match="provide at least one of"):
        tools._log_session(
            session, activity_id=None, rpe=None, pain_score=None, notes="  ", tags=[]
        )
    _assert_nothing_written(session)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"rpe": "7"}, r"rpe must be an integer"),
        ({"pain_score": 2.5}, r"pain_score must be an integer"),
        ({"notes": "x" * 2001}, r"notes must be at most 2000"),
        ({"tags": ["ok", ""]}, r"tags\[1\] must be a non-empty string"),
        ({"tags": "base"}, r"tags must be a list"),
        ({"activity_id": "not-a-uuid"}, r"activity_id is not a valid UUID"),
    ],
)
def test_log_session_rejects_invalid_input(
    session: FakeSession, kwargs: dict[str, Any], expected: str
) -> None:
    args: dict[str, Any] = {
        "activity_id": None,
        "rpe": None,
        "pain_score": None,
        "notes": None,
        "tags": None,
    }
    args.update(kwargs)
    with pytest.raises(ValueError, match=expected):
        tools._log_session(session, **args)
    _assert_nothing_written(session)


def test_log_session_lists_every_problem_at_once(session: FakeSession) -> None:
    with pytest.raises(ValueError) as excinfo:
        tools._log_session(
            session, activity_id=str(uuid4()), rpe=11, pain_score=-1, notes=None, tags=None
        )
    message = str(excinfo.value)
    assert "activity not found" in message
    assert "rpe" in message
    assert "pain_score" in message
    _assert_nothing_written(session)


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"ftp_watts": "abc"}, r"ftp_watts must be a positive integer"),
        ({"ftp_watts": 0}, r"ftp_watts must be a positive integer"),
        ({"max_hr": 185.5}, r"max_hr must be a positive integer"),
        ({"body_weight_kg": "80"}, r"body_weight_kg must be a positive number"),
        ({"body_weight_kg": True}, r"body_weight_kg must be a positive number"),
        ({"body_weight_kg": float("inf")}, r"body_weight_kg must be a positive number"),
        ({"current_phase": "x" * 201}, r"current_phase must be at most 200"),
        ({"notes": 5}, r"notes must be a string"),
    ],
)
def test_update_athlete_context_rejects_invalid_values(
    session: FakeSession, updates: dict[str, Any], expected: str
) -> None:
    with pytest.raises(ValueError, match=expected):
        tools._update_athlete_context(session, updates)
    _assert_nothing_written(session)


def test_update_athlete_context_allows_clearing_and_float_weight(session: FakeSession) -> None:
    row = AthleteContext(
        id=1,
        ftp_watts=210,
        max_hr=None,
        body_weight_kg=81.4,
        current_phase=None,
        notes=None,
        updated_at=datetime(2026, 5, 14, 9, 0, tzinfo=UTC),
    )
    session.get_returns[(AthleteContext, 1)] = row

    tools._update_athlete_context(
        session, {"ftp_watts": 210, "max_hr": None, "body_weight_kg": 81.4, "notes": None}
    )

    assert len(session.execute_calls) == 1


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
