from datetime import UTC, date, datetime

from training_pipeline.derived.weekly_load import compute_weekly_loads


def _activity(*, start: datetime, sport: str | None, load: float | None) -> dict[str, object]:
    return {"start_time": start, "sport_type": sport, "training_load": load}


def test_empty_input_returns_empty() -> None:
    assert compute_weekly_loads([]) == []


def test_groups_by_iso_monday() -> None:
    # 2026-05-13 is a Wednesday → ISO Monday is 2026-05-11.
    result = compute_weekly_loads(
        [
            _activity(start=datetime(2026, 5, 13, 18, tzinfo=UTC), sport="cycling", load=80.0),
            _activity(start=datetime(2026, 5, 14, 7, tzinfo=UTC), sport="cycling", load=50.0),
        ]
    )
    weeks = {(row.week_of, row.sport): row.load for row in result}
    assert weeks[(date(2026, 5, 11), "cycling")] == 130.0
    assert weeks[(date(2026, 5, 11), "total")] == 130.0


def test_tracked_and_untracked_sports_both_hit_total() -> None:
    result = compute_weekly_loads(
        [
            _activity(start=datetime(2026, 5, 11, 18, tzinfo=UTC), sport="cycling", load=100.0),
            _activity(start=datetime(2026, 5, 12, 18, tzinfo=UTC), sport="swimming", load=40.0),
            _activity(start=datetime(2026, 5, 13, 18, tzinfo=UTC), sport=None, load=20.0),
            _activity(start=datetime(2026, 5, 14, 18, tzinfo=UTC), sport="lifting", load=30.0),
        ]
    )
    weeks = {(row.week_of, row.sport): row.load for row in result}
    assert weeks[(date(2026, 5, 11), "cycling")] == 100.0
    assert weeks[(date(2026, 5, 11), "lifting")] == 30.0
    assert weeks[(date(2026, 5, 11), "total")] == 190.0
    assert ("swimming",) not in {(s,) for (_, s) in weeks}


def test_activities_without_load_are_skipped() -> None:
    result = compute_weekly_loads(
        [
            _activity(start=datetime(2026, 5, 13, 18, tzinfo=UTC), sport="running", load=None),
            _activity(start=datetime(2026, 5, 13, 18, tzinfo=UTC), sport="running", load=25.0),
        ]
    )
    weeks = {(row.week_of, row.sport): row.load for row in result}
    assert weeks[(date(2026, 5, 11), "running")] == 25.0
    assert weeks[(date(2026, 5, 11), "total")] == 25.0


def test_results_are_sorted_by_week_then_sport() -> None:
    result = compute_weekly_loads(
        [
            _activity(start=datetime(2026, 5, 13, 18, tzinfo=UTC), sport="cycling", load=10.0),
            _activity(start=datetime(2026, 5, 6, 18, tzinfo=UTC), sport="cycling", load=10.0),
        ]
    )
    weeks_in_order = [row.week_of for row in result]
    assert weeks_in_order == sorted(weeks_in_order)
