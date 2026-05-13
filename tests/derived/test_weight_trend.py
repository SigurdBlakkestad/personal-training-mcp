from datetime import date

from training_pipeline.derived.weight_trend import compute_weight_trend


def test_empty_input_returns_empty() -> None:
    assert compute_weight_trend([]) == []


def test_single_measurement_yields_single_point() -> None:
    series = compute_weight_trend([(date(2026, 5, 1), 80.0)])
    assert len(series) == 1
    point = series[0]
    assert point.date == date(2026, 5, 1)
    assert point.weight_7d_avg == 80.0
    assert point.weight_28d_avg == 80.0


def test_seven_day_window_is_trailing() -> None:
    measurements = [
        (date(2026, 5, 1), 80.0),
        (date(2026, 5, 8), 78.0),
    ]
    series = compute_weight_trend(measurements)
    # On 2026-05-08 the 7d window covers May 2..8 which only contains the 78 reading.
    last = series[-1]
    assert last.date == date(2026, 5, 8)
    assert last.weight_7d_avg == 78.0
    # The 28d window covers Apr 11..May 8 which contains both readings.
    assert last.weight_28d_avg == 79.0


def test_same_day_measurements_collapse_to_last() -> None:
    series = compute_weight_trend(
        [
            (date(2026, 5, 1), 80.0),
            (date(2026, 5, 1), 79.0),
        ]
    )
    assert series[0].weight_7d_avg == 79.0


def test_gap_days_get_average_from_remaining_window() -> None:
    measurements = [(date(2026, 5, 1), 80.0), (date(2026, 5, 3), 82.0)]
    series = compute_weight_trend(measurements)
    # On 5/2 the 7d window covers 4/26..5/2 — only contains the 80 reading.
    may_2 = next(p for p in series if p.date == date(2026, 5, 2))
    assert may_2.weight_7d_avg == 80.0
    # On 5/3 the window contains both.
    may_3 = next(p for p in series if p.date == date(2026, 5, 3))
    assert may_3.weight_7d_avg == 81.0
