from datetime import UTC, date, datetime, tzinfo

import pytest

from training_pipeline.shared import local_time


def _freeze_now(monkeypatch: pytest.MonkeyPatch, frozen: datetime) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "FrozenDatetime":
            moment = frozen.astimezone(tz) if tz is not None else frozen
            return cls.fromtimestamp(moment.timestamp(), tz=moment.tzinfo)

    monkeypatch.setattr(local_time, "datetime", FrozenDatetime)


def test_local_today_is_already_tomorrow_late_evening_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 23:30Z on the 29th is 01:30 on the 30th in Oslo (CEST).
    _freeze_now(monkeypatch, datetime(2026, 9, 29, 23, 30, tzinfo=UTC))
    assert local_time.local_today() == date(2026, 9, 30)


def test_local_date_uses_athlete_timezone() -> None:
    assert local_time.local_date(datetime(2026, 9, 27, 22, 30, tzinfo=UTC)) == date(2026, 9, 28)
    # Winter (CET, UTC+1): 22:30Z is still the same local day.
    assert local_time.local_date(datetime(2026, 1, 10, 22, 30, tzinfo=UTC)) == date(2026, 1, 10)
