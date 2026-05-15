from __future__ import annotations

import base64
import io
import os
import tarfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from training_pipeline.ingestors.base import IngestionResult
from training_pipeline.ingestors.garmin import (
    GARMIN_DEFAULT_LOOKBACK_DAYS,
    GARMIN_PAGE_SIZE,
    GarminIngestor,
    _extract_hrv_ms,
    _extract_intensity_minutes,
    _extract_readiness,
    _extract_respiration_avg,
    _extract_sleep_duration,
    _extract_sleep_score,
    _extract_vo2_max,
    _normalize_sport,
    _parse_garmin_time,
    decode_tokens_to_dir,
)


def _garmin_activity(
    activity_id: int = 1,
    type_key: str = "road_biking",
    start: str = "2026-04-01 10:00:00",
    duration: float = 3600.0,
) -> dict[str, Any]:
    return {
        "activityId": activity_id,
        "activityName": f"Activity {activity_id}",
        "activityType": {"typeKey": type_key},
        "startTimeGMT": start,
        "duration": duration,
        "distance": 30000.0,
        "elevationGain": 250.0,
        "averageHR": 142.0,
        "maxHR": 175.0,
        "minHR": 95.0,
        "avgPower": 210.0,
        "normPower": 225.0,
        "maxPower": 520.0,
        "maxAvgPower": 320.0,
        "averageBikingCadenceInRevPerMinute": 88.0,
        "calories": 600.0,
        "aerobicTrainingEffect": 3.4,
        "anaerobicTrainingEffect": 1.2,
        "trainingEffectLabel": "TEMPO",
        "vO2MaxValue": 53.5,
        "moderateIntensityMinutes": 20,
        "vigorousIntensityMinutes": 40,
        "avgStrideLength": 152.3,
        "avgGroundContactTime": 245.0,
    }


def _make_session(*, upsert_inserted: bool = True) -> MagicMock:
    session = MagicMock(spec=Session)
    session.scalar.return_value = None
    session.execute.return_value.scalar_one.return_value = upsert_inserted
    return session


def test_normalize_sport_known_and_unknown() -> None:
    assert _normalize_sport("road_biking") == "cycling"
    assert _normalize_sport("indoor_cycling") == "cycling"
    assert _normalize_sport("running") == "running"
    assert _normalize_sport("trail_running") == "running"
    assert _normalize_sport("lap_swimming") == "swimming"
    assert _normalize_sport("strength_training") == "lifting"
    assert _normalize_sport("hiking") == "walking"
    assert _normalize_sport("yoga") == "other"
    assert _normalize_sport(None) == "other"
    assert _normalize_sport("") == "other"


def test_parse_garmin_time_accepts_space_and_iso() -> None:
    assert _parse_garmin_time("2026-04-01 10:00:00") == datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    assert _parse_garmin_time("2026-04-01T10:00:00Z") == datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    assert _parse_garmin_time(None) is None
    assert _parse_garmin_time("") is None
    assert _parse_garmin_time("not-a-date") is None


def test_decode_tokens_to_dir_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / ".garminconnect"
    src.mkdir()
    (src / "oauth1_token.json").write_text('{"foo": "bar"}')
    (src / "oauth2_token.json").write_text('{"baz": "qux"}')

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(src), arcname=".garminconnect")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    tokens_path = decode_tokens_to_dir(b64)

    assert os.path.isdir(tokens_path)
    assert tokens_path.endswith(".garminconnect")
    assert os.path.isfile(os.path.join(tokens_path, "oauth1_token.json"))
    assert os.path.isfile(os.path.join(tokens_path, "oauth2_token.json"))


def test_map_activity_basic_fields() -> None:
    ingestor = GarminIngestor(client=MagicMock())
    activity = _garmin_activity()
    start_time = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    mapped = ingestor._map_activity(activity, start_time)

    assert mapped["source"] == "garmin"
    assert mapped["source_id"] == "1"
    assert mapped["start_time"] == start_time
    assert mapped["end_time"] == datetime(2026, 4, 1, 11, 0, tzinfo=UTC)
    assert mapped["sport_type"] == "cycling"
    assert mapped["name"] == "Activity 1"
    assert mapped["duration_seconds"] == 3600
    assert mapped["distance_meters"] == 30000.0
    assert mapped["elevation_gain_meters"] == 250.0
    assert mapped["avg_hr"] == 142
    assert mapped["max_hr"] == 175
    assert mapped["avg_power"] == 210
    assert mapped["normalized_power"] == 225
    assert mapped["avg_cadence"] == 88
    assert mapped["calories"] == 600
    assert mapped["aerobic_training_effect"] == 3.4
    assert mapped["anaerobic_training_effect"] == 1.2
    assert mapped["training_effect_label"] == "TEMPO"
    assert mapped["vo2_max"] == 53.5
    assert mapped["moderate_intensity_minutes"] == 20
    assert mapped["vigorous_intensity_minutes"] == 40
    assert mapped["min_hr"] == 95
    assert mapped["max_power"] == 520
    assert mapped["avg_stride_length_cm"] == 152.3
    assert mapped["avg_ground_contact_time_ms"] == 245
    assert mapped["raw"] is activity


def test_map_activity_handles_missing_optional_fields() -> None:
    ingestor = GarminIngestor(client=MagicMock())
    minimal = {
        "activityId": 99,
        "activityName": "Bare",
        "activityType": {"typeKey": "running"},
        "startTimeGMT": "2026-04-02 07:30:00",
    }
    start_time = datetime(2026, 4, 2, 7, 30, tzinfo=UTC)
    mapped = ingestor._map_activity(minimal, start_time)
    assert mapped["duration_seconds"] is None
    assert mapped["end_time"] is None
    assert mapped["avg_hr"] is None
    assert mapped["avg_cadence"] is None
    assert mapped["sport_type"] == "running"


def test_sync_activities_inserts_new_activity_when_no_strava_match() -> None:
    client = MagicMock()
    client.get_activities.side_effect = [[_garmin_activity(1)], []]

    ingestor = GarminIngestor(client=client)
    ingestor._activity_exists = MagicMock(return_value=False)  # type: ignore[method-assign]
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=False)  # type: ignore[method-assign]
    captured: list[dict[str, Any]] = []
    original_upsert = ingestor.upsert_activity

    def capture(s: Any, payload: dict[str, Any]) -> str:
        captured.append(payload)
        return original_upsert(s, payload)

    ingestor.upsert_activity = capture  # type: ignore[method-assign]

    session = _make_session()
    result = IngestionResult()
    ingestor._sync_activities(
        client, session, datetime(2020, 1, 1, tzinfo=UTC), result, MagicMock()
    )

    assert result.records_processed == 1
    assert result.records_inserted == 1
    assert captured[0]["source_id"] == "1"


def test_sync_activities_stops_when_existing_id_seen() -> None:
    client = MagicMock()
    client.get_activities.return_value = [
        _garmin_activity(10),
        _garmin_activity(20),
        _garmin_activity(30),
    ]

    ingestor = GarminIngestor(client=client)
    seen = {"20"}
    ingestor._activity_exists = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda s, sid: sid in seen
    )
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=False)  # type: ignore[method-assign]

    session = _make_session()
    result = IngestionResult()
    ingestor._sync_activities(
        client, session, datetime(2020, 1, 1, tzinfo=UTC), result, MagicMock()
    )

    assert result.records_processed == 1
    assert result.records_inserted == 1


def test_sync_activities_stops_when_older_than_since() -> None:
    client = MagicMock()
    client.get_activities.return_value = [
        _garmin_activity(1, start="2026-04-10 10:00:00"),
        _garmin_activity(2, start="2026-04-05 10:00:00"),
    ]
    ingestor = GarminIngestor(client=client)
    ingestor._activity_exists = MagicMock(return_value=False)  # type: ignore[method-assign]
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=False)  # type: ignore[method-assign]

    session = _make_session()
    result = IngestionResult()
    since = datetime(2026, 4, 8, tzinfo=UTC)
    ingestor._sync_activities(client, session, since, result, MagicMock())

    assert result.records_processed == 1


def test_merge_into_strava_writes_supplement_on_match() -> None:
    ingestor = GarminIngestor(client=MagicMock())
    start_time = datetime(2026, 4, 1, 10, 0, 30, tzinfo=UTC)
    garmin_mapped = {
        "source": "garmin",
        "source_id": "g42",
        "start_time": start_time,
        "aerobic_training_effect": 3.4,
        "anaerobic_training_effect": 1.1,
        "training_effect_label": "TEMPO",
        "vo2_max": 52.0,
        "moderate_intensity_minutes": 15,
        "vigorous_intensity_minutes": 25,
        "min_hr": 92,
        "avg_stride_length_cm": 148.0,
        "avg_ground_contact_time_ms": 240,
        "raw": {"activityId": 42, "extra": "garmin-data"},
    }

    strava_row = MagicMock()
    strava_row.source_id = "s99"
    strava_row.raw = {"id": 99}
    strava_row.garmin_supplement = None
    strava_row.aerobic_training_effect = None
    strava_row.anaerobic_training_effect = None
    strava_row.training_effect_label = None
    strava_row.vo2_max = None
    strava_row.moderate_intensity_minutes = None
    strava_row.vigorous_intensity_minutes = None
    strava_row.min_hr = None
    strava_row.avg_stride_length_cm = None
    strava_row.avg_ground_contact_time_ms = None

    session = MagicMock(spec=Session)
    session.scalar.return_value = strava_row

    merged = ingestor._merge_into_strava_if_exists(session, garmin_mapped, MagicMock())

    assert merged is True
    assert strava_row.raw == {"id": 99}  # untouched
    assert strava_row.garmin_supplement == garmin_mapped["raw"]
    assert strava_row.aerobic_training_effect == 3.4
    assert strava_row.anaerobic_training_effect == 1.1
    assert strava_row.training_effect_label == "TEMPO"
    assert strava_row.vo2_max == 52.0
    assert strava_row.moderate_intensity_minutes == 15
    assert strava_row.vigorous_intensity_minutes == 25
    assert strava_row.min_hr == 92
    assert strava_row.avg_stride_length_cm == 148.0
    assert strava_row.avg_ground_contact_time_ms == 240


def test_merge_into_strava_does_not_overwrite_existing_columns() -> None:
    ingestor = GarminIngestor(client=MagicMock())
    garmin_mapped = {
        "source": "garmin",
        "source_id": "g42",
        "start_time": datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        "aerobic_training_effect": 3.4,
        "min_hr": 92,
        "raw": {"activityId": 42},
    }
    strava_row = MagicMock()
    strava_row.raw = {"id": 99}
    strava_row.aerobic_training_effect = 4.1  # pre-existing value wins
    strava_row.min_hr = None
    # touch defaults for the other GARMIN_ONLY fields so getattr returns None
    for field in (
        "anaerobic_training_effect",
        "training_effect_label",
        "vo2_max",
        "moderate_intensity_minutes",
        "vigorous_intensity_minutes",
        "avg_stride_length_cm",
        "avg_ground_contact_time_ms",
    ):
        setattr(strava_row, field, None)
    session = MagicMock(spec=Session)
    session.scalar.return_value = strava_row

    ingestor._merge_into_strava_if_exists(session, garmin_mapped, MagicMock())

    assert strava_row.aerobic_training_effect == 4.1  # unchanged
    assert strava_row.min_hr == 92  # filled because it was None


def test_merge_into_strava_returns_false_when_no_match() -> None:
    ingestor = GarminIngestor(client=MagicMock())
    garmin_mapped = {
        "source": "garmin",
        "source_id": "g42",
        "start_time": datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        "raw": {"activityId": 42},
    }
    session = MagicMock(spec=Session)
    session.scalar.return_value = None

    assert ingestor._merge_into_strava_if_exists(session, garmin_mapped, MagicMock()) is False


def test_extract_sleep_score_handles_shapes() -> None:
    full = {"dailySleepDTO": {"sleepScores": {"overall": {"value": 82}}, "sleepTimeSeconds": 28800}}
    assert _extract_sleep_score(full) == 82
    assert _extract_sleep_duration(full) == 28800
    assert _extract_sleep_score(None) is None
    assert _extract_sleep_score({}) is None
    assert _extract_sleep_score({"dailySleepDTO": {}}) is None


def test_extract_hrv_ms() -> None:
    assert _extract_hrv_ms({"hrvSummary": {"lastNightAvg": 54.2}}) == 54.2
    assert _extract_hrv_ms(None) is None
    assert _extract_hrv_ms({}) is None
    assert _extract_hrv_ms({"hrvSummary": {}}) is None


def test_sync_daily_summaries_extracts_fields() -> None:
    client = MagicMock()
    client.get_user_summary.return_value = {
        "restingHeartRate": 48,
        "averageStressLevel": 22,
        "maxStressLevel": 78,
        "bodyBatteryHighestValue": 95,
        "bodyBatteryLowestValue": 30,
        "totalSteps": 11500,
        "activeKilocalories": 920,
    }
    client.get_sleep_data.return_value = {
        "dailySleepDTO": {
            "sleepScores": {"overall": {"value": 78}},
            "sleepTimeSeconds": 25200,
        }
    }
    client.get_hrv_data.return_value = {"hrvSummary": {"lastNightAvg": 61.5}}
    client.get_training_readiness.return_value = [
        {"score": 70, "level": "MODERATE", "inputContext": "AFTER_WAKEUP_RESET"}
    ]
    client.get_max_metrics.return_value = [
        {
            "generic": {"vo2MaxPreciseValue": 47.2},
            "cycling": {"vo2MaxPreciseValue": 53.6},
        }
    ]
    client.get_intensity_minutes_data.return_value = {
        "moderateMinutes": 80,
        "vigorousMinutes": 45,
    }
    client.get_respiration_data.return_value = {
        "avgWakingRespirationValue": 14.2,
        "lowestRespirationValue": 11,
    }

    fixed_now = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
    ingestor = GarminIngestor(client=client, now=lambda: fixed_now)
    captured: list[dict[str, Any]] = []

    def capture(s: Any, payload: dict[str, Any]) -> str:
        captured.append(payload)
        return "inserted"

    ingestor.upsert_daily_summary = capture  # type: ignore[method-assign]

    session = _make_session()
    result = IngestionResult()
    since = datetime(2026, 4, 2, 0, 0, tzinfo=UTC)
    ingestor._sync_daily_summaries(client, session, since, result, MagicMock())

    assert len(captured) == 1
    row = captured[0]
    assert row["date"] == date(2026, 4, 2)
    assert row["source"] == "garmin"
    assert row["sleep_score"] == 78
    assert row["sleep_duration_seconds"] == 25200
    assert row["resting_hr"] == 48
    assert row["hrv_ms"] == 61.5
    assert row["stress_avg"] == 22
    assert row["stress_max"] == 78
    assert row["body_battery_high"] == 95
    assert row["body_battery_low"] == 30
    assert row["steps"] == 11500
    assert row["active_calories"] == 920
    assert row["training_readiness_score"] == 70
    assert row["training_readiness_level"] == "MODERATE"
    assert row["vo2_max_running"] == 47.2
    assert row["vo2_max_cycling"] == 53.6
    assert row["intensity_minutes_moderate"] == 80
    assert row["intensity_minutes_vigorous"] == 45
    assert row["respiration_avg"] == 14.2


def test_extract_readiness_handles_shapes() -> None:
    assert _extract_readiness(None) == (None, None)
    assert _extract_readiness([]) == (None, None)
    assert _extract_readiness({"score": 80, "level": "READY"}) == (80, "READY")
    morning = [
        {"score": 55, "level": "LOW", "inputContext": "EVENING"},
        {"score": 72, "level": "READY", "inputContext": "AFTER_WAKEUP_RESET"},
    ]
    assert _extract_readiness(morning) == (72, "READY")
    # falls back to first entry when no morning context found
    assert _extract_readiness([{"score": 64, "level": "MODERATE"}]) == (64, "MODERATE")


def test_extract_vo2_max_handles_shapes() -> None:
    assert _extract_vo2_max(None) == (None, None)
    assert _extract_vo2_max([]) == (None, None)
    payload = [{"generic": {"vo2MaxPreciseValue": 48.1}, "cycling": {"vo2MaxValue": 55}}]
    assert _extract_vo2_max(payload) == (48.1, 55.0)
    # dict form (some firmware variants)
    assert _extract_vo2_max({"generic": {"vo2MaxValue": 50}}) == (50.0, None)


def test_extract_intensity_minutes_handles_missing() -> None:
    assert _extract_intensity_minutes(None) == (None, None)
    assert _extract_intensity_minutes({"moderateMinutes": 30}) == (30, None)
    assert _extract_intensity_minutes({"moderateMinutes": 30, "vigorousMinutes": 15}) == (30, 15)


def test_extract_respiration_avg_prefers_waking() -> None:
    assert _extract_respiration_avg(None) is None
    assert _extract_respiration_avg({}) is None
    assert (
        _extract_respiration_avg({"avgWakingRespirationValue": 14, "avgSleepRespirationValue": 12})
        == 14.0
    )
    assert _extract_respiration_avg({"avgSleepRespirationValue": 12}) == 12.0


def test_sync_activities_backfill_does_not_stop_on_existing_id() -> None:
    client = MagicMock()
    client.get_activities.side_effect = [
        [_garmin_activity(1), _garmin_activity(2), _garmin_activity(3)],
        [],
    ]
    ingestor = GarminIngestor(client=client, backfill=True)
    # Pretend all activities already exist; backfill should re-process them anyway.
    ingestor._activity_exists = MagicMock(return_value=True)  # type: ignore[method-assign]
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=False)  # type: ignore[method-assign]

    session = _make_session()
    result = IngestionResult()
    ingestor._sync_activities(
        client, session, datetime(2020, 1, 1, tzinfo=UTC), result, MagicMock()
    )

    assert result.records_processed == 3


def test_sync_daily_summaries_skips_when_all_endpoints_return_none() -> None:
    client = MagicMock()
    client.get_user_summary.return_value = None
    client.get_sleep_data.return_value = None
    client.get_hrv_data.return_value = None
    client.get_training_readiness.return_value = None
    client.get_max_metrics.return_value = None
    client.get_intensity_minutes_data.return_value = None
    client.get_respiration_data.return_value = None

    fixed_now = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
    ingestor = GarminIngestor(client=client, now=lambda: fixed_now)
    captured: list[dict[str, Any]] = []
    ingestor.upsert_daily_summary = lambda s, payload: (
        captured.append(  # type: ignore[method-assign]
            payload
        )
        or "inserted"
    )

    session = _make_session()
    result = IngestionResult()
    since = datetime(2026, 4, 2, 0, 0, tzinfo=UTC)
    ingestor._sync_daily_summaries(client, session, since, result, MagicMock())

    assert captured == []
    assert result.records_processed == 0


def test_safe_call_returns_none_on_exception() -> None:
    ingestor = GarminIngestor(client=MagicMock())

    def boom(_iso: str) -> Any:
        raise RuntimeError("garmin endpoint down")

    log = MagicMock()
    assert ingestor._safe_call(boom, "2026-04-01", log, "user_summary") is None
    log.warning.assert_called_once()


def test_initialize_client_raises_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSettings:
        GARMINTOKENS_B64 = ""

    monkeypatch.setattr("training_pipeline.ingestors.garmin.get_settings", lambda: FakeSettings())
    ingestor = GarminIngestor()
    with pytest.raises(RuntimeError, match="GARMINTOKENS_B64"):
        ingestor._initialize_client()


def test_initialize_client_uses_factory_when_provided(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / ".garminconnect"
    src.mkdir()
    (src / "oauth1_token.json").write_text("{}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(src), arcname=".garminconnect")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    class FakeSettings:
        GARMINTOKENS_B64 = b64

    monkeypatch.setattr("training_pipeline.ingestors.garmin.get_settings", lambda: FakeSettings())

    captured_path: list[str] = []
    fake_client = object()

    def factory(path: str) -> object:
        captured_path.append(path)
        return fake_client

    ingestor = GarminIngestor(client_factory=factory)
    client = ingestor._initialize_client()
    assert client is fake_client
    assert captured_path[0].endswith(".garminconnect")


def test_compute_since_defaults_to_30_days_ago() -> None:
    fixed_now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    ingestor = GarminIngestor(client=MagicMock(), now=lambda: fixed_now)
    session = MagicMock(spec=Session)
    session.scalar.return_value = None

    since = ingestor._compute_since(session)
    expected = fixed_now - timedelta(days=GARMIN_DEFAULT_LOOKBACK_DAYS)
    assert since == expected


def test_sync_activities_paginates_until_short_page() -> None:
    client = MagicMock()
    page_one = [_garmin_activity(i) for i in range(GARMIN_PAGE_SIZE)]
    page_two = [_garmin_activity(GARMIN_PAGE_SIZE + 1)]
    client.get_activities.side_effect = [page_one, page_two, []]

    ingestor = GarminIngestor(client=client)
    ingestor._activity_exists = MagicMock(return_value=False)  # type: ignore[method-assign]
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=False)  # type: ignore[method-assign]

    session = _make_session()
    result = IngestionResult()
    ingestor._sync_activities(
        client, session, datetime(2020, 1, 1, tzinfo=UTC), result, MagicMock()
    )

    assert result.records_processed == GARMIN_PAGE_SIZE + 1
    # Should have made exactly 2 pagination calls (second one returns short page → stop)
    assert client.get_activities.call_count == 2
