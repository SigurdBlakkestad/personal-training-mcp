import math

from training_pipeline.derived.training_load import (
    compute_training_load,
    compute_trimp,
    compute_tss_from_power,
)


def test_tss_at_threshold_one_hour_is_100() -> None:
    # Riding at NP=FTP for one hour by definition equals 100 TSS.
    assert compute_tss_from_power(duration_seconds=3600, normalized_power=200, ftp=200) == 100.0


def test_tss_half_hour_at_threshold_is_50() -> None:
    assert compute_tss_from_power(duration_seconds=1800, normalized_power=200, ftp=200) == 50.0


def test_tss_scales_quadratically_with_intensity() -> None:
    # NP at 0.5*FTP for an hour should yield 25 TSS (IF^2 * 100).
    assert compute_tss_from_power(duration_seconds=3600, normalized_power=100, ftp=200) == 25.0


def test_trimp_returns_positive_for_typical_endurance_hour() -> None:
    value = compute_trimp(duration_seconds=3600, avg_hr=140, rest_hr=50, max_hr=190)
    assert value > 0
    # Sanity: a 60 min endurance ride should land in a reasonable TRIMP range.
    assert 30 < value < 200


def test_trimp_returns_zero_when_avg_hr_at_or_below_rest() -> None:
    assert compute_trimp(duration_seconds=3600, avg_hr=50, rest_hr=50, max_hr=190) == 0.0
    assert compute_trimp(duration_seconds=3600, avg_hr=40, rest_hr=50, max_hr=190) == 0.0


def test_trimp_handles_degenerate_hr_range() -> None:
    assert compute_trimp(duration_seconds=3600, avg_hr=180, rest_hr=190, max_hr=190) == 0.0


def test_compute_training_load_prefers_power_when_available() -> None:
    load = compute_training_load(
        {"duration_seconds": 3600, "normalized_power": 200, "avg_hr": 140}, ftp=200
    )
    assert load == 100.0


def test_compute_training_load_falls_back_to_hr() -> None:
    load = compute_training_load(
        {"duration_seconds": 3600, "normalized_power": None, "avg_hr": 140}, ftp=200
    )
    assert load is not None
    expected = compute_trimp(3600, 140)
    assert math.isclose(load, expected)


def test_compute_training_load_returns_none_without_signals() -> None:
    assert (
        compute_training_load(
            {"duration_seconds": 3600, "normalized_power": None, "avg_hr": None}, ftp=200
        )
        is None
    )


def test_compute_training_load_returns_none_without_duration() -> None:
    assert (
        compute_training_load(
            {"duration_seconds": None, "normalized_power": 200, "avg_hr": 140}, ftp=200
        )
        is None
    )
    assert (
        compute_training_load(
            {"duration_seconds": 0, "normalized_power": 200, "avg_hr": 140}, ftp=200
        )
        is None
    )


def test_compute_training_load_ignores_zero_power() -> None:
    load = compute_training_load(
        {"duration_seconds": 3600, "normalized_power": 0, "avg_hr": 140}, ftp=200
    )
    assert load is not None
    assert math.isclose(load, compute_trimp(3600, 140))
