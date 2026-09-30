import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from training_pipeline.derived import compute
from training_pipeline.derived.training_load import compute_trimp
from training_pipeline.shared.local_time import local_today
from training_pipeline.shared.models import Activity


class FakeResult:
    def all(self) -> list[Any]:
        return []


class FakeSession:
    """Minimal stand-in for a SQLAlchemy Session.

    `scalars` answers the activity query; every other statement (metric upserts,
    the body-measurement read) is a no-op returning an empty result.
    """

    def __init__(self, activities: list[Activity]) -> None:
        self._activities = activities
        self.flush_calls = 0

    def scalars(self, stmt: Any) -> list[Activity]:
        return self._activities

    def flush(self) -> None:
        self.flush_calls += 1

    def execute(self, stmt: Any) -> FakeResult:
        return FakeResult()


class FakeSettings:
    # Deliberately different from both the hardcoded training_load defaults
    # (50/190) and the shipped settings defaults (49/193).
    ATHLETE_FTP = 210
    ATHLETE_HR_REST = 44
    ATHLETE_HR_MAX = 201


def _activity(**overrides: Any) -> Activity:
    fields: dict[str, Any] = {
        "source": "garmin",
        "source_id": "a1",
        "start_time": datetime.now(UTC) - timedelta(days=3),
        "sport_type": "cycling",
        "duration_seconds": 3600,
        "normalized_power": None,
        "avg_hr": 140,
        "training_load": None,
    }
    fields.update(overrides)
    return Activity(**fields)


def test_recompute_all_uses_athlete_hr_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def spy(activity: Any, **kwargs: Any) -> float:
        calls.append(kwargs)
        return 42.0

    monkeypatch.setattr(compute, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(compute, "compute_training_load", spy)

    session = FakeSession([_activity()])
    counts = compute.recompute_all(session)

    assert counts.training_load_updated == 1
    assert len(calls) == 1
    assert calls[0]["ftp"] == 210
    assert calls[0]["rest_hr"] == 44
    assert calls[0]["max_hr"] == 201


def test_recompute_all_force_does_not_shrink_other_metrics_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forcing a training-load recompute must not narrow the other windows.

    `recompute_all(force=True)` scopes the training_load overwrite to the last
    `FORCE_RECOMPUTE_WINDOW_DAYS` days, but CTL/ATL/TSB, weekly load, and
    weight trend must still use the normal `since` (the 365-day default here,
    since none was passed) — not the narrower force window.
    """
    since_seen: dict[str, Any] = {}

    def record_since(name: str) -> Any:
        def _fn(*args: Any, **kwargs: Any) -> int:
            since_seen[name] = kwargs.get("since")
            return 0

        return _fn

    monkeypatch.setattr(compute, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(compute, "_upsert_ctl_atl_tsb", record_since("ctl_atl_tsb"))
    monkeypatch.setattr(compute, "_upsert_weekly_load", record_since("weekly_load"))
    monkeypatch.setattr(compute, "_upsert_weight_trend", record_since("weight_trend"))

    session = FakeSession([_activity()])
    compute.recompute_all(session, force=True)

    today = local_today()
    expected_default_since = today - timedelta(days=compute.DEFAULT_RECOMPUTE_WINDOW_DAYS)
    assert since_seen["ctl_atl_tsb"] == expected_default_since
    assert since_seen["weekly_load"] == expected_default_since
    assert since_seen["weight_trend"] == expected_default_since


def test_recompute_all_extends_ctl_atl_tsb_through_local_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(compute, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(compute, "_upsert_metrics", lambda session, rows: written.extend(rows))

    compute.recompute_all(FakeSession([_activity(training_load=80.0)]))

    ctl_dates = sorted(row["date"] for row in written if row["metric_name"] == "ctl")
    assert ctl_dates[-1] == local_today()


def test_backfill_training_load_skips_existing_values_by_default() -> None:
    activity = _activity(training_load=12.5)

    updated = compute._backfill_training_load([activity], ftp=210, rest_hr=44, max_hr=201)

    assert updated == 0
    assert activity.training_load == 12.5


def test_backfill_training_load_overwrites_existing_values_when_forced() -> None:
    activity = _activity(training_load=12.5)

    updated = compute._backfill_training_load(
        [activity], ftp=210, rest_hr=44, max_hr=201, force=True
    )

    assert updated == 1
    assert activity.training_load is not None
    assert activity.training_load != 12.5


def test_backfill_training_load_force_since_excludes_older_warmup_activities() -> None:
    """A forced recompute must not overwrite history outside its window.

    `recompute_all` pulls in an EWMA warmup window older than the requested
    `since` for CTL/ATL context. Forcing a recompute of the last 60 days must
    not silently rewrite training_load for those older warmup-only activities.
    """
    today = datetime.now(UTC).date()
    force_since = today - timedelta(days=60)
    old_activity = _activity(
        source_id="old",
        start_time=datetime.combine(force_since - timedelta(days=1), datetime.min.time(), UTC),
        training_load=12.5,
    )
    recent_activity = _activity(
        source_id="recent",
        start_time=datetime.combine(force_since, datetime.min.time(), UTC),
        training_load=12.5,
    )

    updated = compute._backfill_training_load(
        [old_activity, recent_activity],
        ftp=210,
        rest_hr=44,
        max_hr=201,
        force=True,
        force_since=force_since,
    )

    assert updated == 1
    assert old_activity.training_load == 12.5
    assert recent_activity.training_load is not None
    assert recent_activity.training_load != 12.5


def test_backfill_training_load_scores_run_power_as_trimp() -> None:
    run = _activity(sport_type="running", normalized_power=334, avg_hr=136, duration_seconds=1740)
    ride = _activity(source_id="a2", normalized_power=210)

    updated = compute._backfill_training_load([run, ride], ftp=210, rest_hr=44, max_hr=201)

    assert updated == 2
    assert run.training_load is not None
    assert math.isclose(run.training_load, compute_trimp(1740, 136, rest_hr=44, max_hr=201))
    assert ride.training_load == 100.0
