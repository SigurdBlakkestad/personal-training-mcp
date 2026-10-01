from datetime import UTC, datetime

from training_pipeline.cli import _parse_since


def test_parse_since_starts_at_athlete_local_midnight() -> None:
    # Oslo midnight (CEST) is 22:00Z the previous day; UTC midnight would skip
    # the first two local hours of the requested day.
    assert _parse_since("2026-09-29") == datetime(2026, 9, 28, 22, 0, tzinfo=UTC)


def test_parse_since_none() -> None:
    assert _parse_since(None) is None
