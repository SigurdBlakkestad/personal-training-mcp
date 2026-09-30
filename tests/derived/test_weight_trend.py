from datetime import UTC, date, datetime

from training_pipeline.derived.weight_trend import compute_weight_trend, daily_weights


def test_empty_input_returns_empty() -> None:
    assert compute_weight_trend([]) == []


def test_single_measurement_yields_single_point() -> None:
    series = compute_weight_trend([(datetime(2026, 5, 1, 7, tzinfo=UTC), 80.0)])
    assert len(series) == 1
    point = series[0]
    assert point.date == date(2026, 5, 1)
    assert point.weight_7d_avg == 80.0
    assert point.weight_28d_avg == 80.0


def test_seven_day_window_is_trailing() -> None:
    measurements = [
        (datetime(2026, 5, 1, 7, tzinfo=UTC), 80.0),
        (datetime(2026, 5, 8, 7, tzinfo=UTC), 78.0),
    ]
    series = compute_weight_trend(measurements)
    # On 2026-05-08 the 7d window covers May 2..8 which only contains the 78 reading.
    last = series[-1]
    assert last.date == date(2026, 5, 8)
    assert last.weight_7d_avg == 78.0
    # The 28d window covers Apr 11..May 8 which contains both readings.
    assert last.weight_28d_avg == 79.0


def test_same_day_measurements_collapse_to_first() -> None:
    # 07:00 and 21:00 Oslo (CEST) — the morning reading is the day's weight,
    # regardless of the order rows arrive in.
    series = compute_weight_trend(
        [
            (datetime(2026, 5, 1, 19, tzinfo=UTC), 79.0),
            (datetime(2026, 5, 1, 5, tzinfo=UTC), 80.0),
        ]
    )
    assert len(series) == 1
    assert series[0].weight_7d_avg == 80.0


def test_daily_weights_buckets_by_local_day() -> None:
    # 23:30Z on May 1 is 01:30 on May 2 in Oslo: it's May 2's first reading.
    by_day = daily_weights(
        [
            (datetime(2026, 5, 1, 5, tzinfo=UTC), 80.0),
            (datetime(2026, 5, 1, 23, 30, tzinfo=UTC), 79.5),
            (datetime(2026, 5, 2, 5, tzinfo=UTC), 79.0),
        ]
    )
    assert by_day == {date(2026, 5, 1): 80.0, date(2026, 5, 2): 79.5}


def test_gap_days_get_average_from_remaining_window() -> None:
    measurements = [
        (datetime(2026, 5, 1, 7, tzinfo=UTC), 80.0),
        (datetime(2026, 5, 3, 7, tzinfo=UTC), 82.0),
    ]
    series = compute_weight_trend(measurements)
    # On 5/2 the 7d window covers 4/26..5/2 — only contains the 80 reading.
    may_2 = next(p for p in series if p.date == date(2026, 5, 2))
    assert may_2.weight_7d_avg == 80.0
    # On 5/3 the window contains both.
    may_3 = next(p for p in series if p.date == date(2026, 5, 3))
    assert may_3.weight_7d_avg == 81.0
