from datetime import date, timedelta

from training_pipeline.derived.ctl_atl_tsb import compute_ctl_atl_tsb


def test_empty_input_returns_empty() -> None:
    assert compute_ctl_atl_tsb([]) == []


def test_single_day_emits_one_point() -> None:
    points = compute_ctl_atl_tsb([(date(2026, 1, 1), 100.0)])
    assert len(points) == 1
    assert points[0].date == date(2026, 1, 1)
    # CTL = 0 + (100 - 0)/42, ATL = 0 + (100 - 0)/7
    assert points[0].ctl == 100.0 / 42
    assert points[0].atl == 100.0 / 7
    assert points[0].tsb == points[0].ctl - points[0].atl


def test_gaps_are_treated_as_zero_load() -> None:
    # Day 1 has load 100, then nothing for 3 days. CTL/ATL must decay through gaps.
    series = compute_ctl_atl_tsb([(date(2026, 1, 1), 100.0), (date(2026, 1, 5), 0.0)])
    assert [p.date for p in series] == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 5),
    ]
    # ATL after day 1 = 100/7; after a zero day, ATL multiplied by 6/7.
    assert series[1].atl < series[0].atl
    assert series[2].atl < series[1].atl


def test_steady_state_ctl_converges_toward_constant_load() -> None:
    # Apply 50 load every day for a long time — CTL and ATL approach 50.
    loads = [(date(2026, 1, 1) + timedelta(days=i), 50.0) for i in range(365)]
    series = compute_ctl_atl_tsb(loads)
    last = series[-1]
    assert 49.0 < last.ctl < 50.0
    assert 49.99 < last.atl < 50.0
    # At steady state TSB ≈ 0.
    assert abs(last.tsb) < 1.0


def test_same_day_loads_are_summed() -> None:
    series = compute_ctl_atl_tsb([(date(2026, 1, 1), 40.0), (date(2026, 1, 1), 60.0)])
    assert len(series) == 1
    assert series[0].ctl == 100.0 / 42
