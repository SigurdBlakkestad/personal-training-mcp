from __future__ import annotations

import base64
import io
import os
import tarfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

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
    _parse_exercise_sets,
    _parse_garmin_time,
    _parse_laps,
    _top_exercise,
    decode_tokens_to_dir,
)
from training_pipeline.shared.models import ActivityExerciseSet, ActivityLap


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
        "activityTrainingLoad": 187.4,
    }


def _make_session(*, upsert_inserted: bool = True) -> MagicMock:
    session = MagicMock(spec=Session)
    session.scalar.return_value = None
    session.execute.return_value.scalar_one.return_value = upsert_inserted
    return session


class _FakeGarminClient:
    """Stands in for garminconnect's Garmin, whose token serializer lives on
    the inner ``.client``."""

    def __init__(self, payload: str) -> None:
        self.client = _FakeInnerClient(payload)


class _FakeInnerClient:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def dumps(self) -> str:
        return self._payload


def _stub_token_store(monkeypatch: pytest.MonkeyPatch, *, stored: str | None) -> list[str]:
    """Replace the DB-backed token store with in-memory stubs.

    Returns the list that persisted payloads are appended to.
    """
    saved: list[str] = []
    monkeypatch.setattr("training_pipeline.ingestors.garmin.load_stored_tokens", lambda: stored)
    monkeypatch.setattr("training_pipeline.ingestors.garmin.save_stored_tokens", saved.append)
    return saved


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
    assert mapped["garmin_training_load"] == 187.4
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
    assert mapped["garmin_training_load"] is None
    assert mapped["sport_type"] == "running"


def test_sync_activities_inserts_new_activity_when_no_strava_match() -> None:
    client = MagicMock()
    client.get_activities.side_effect = [[_garmin_activity(1)], []]

    ingestor = GarminIngestor(client=client)
    ingestor._activity_exists = MagicMock(return_value=False)  # type: ignore[method-assign]
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=None)  # type: ignore[method-assign]
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
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=None)  # type: ignore[method-assign]

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
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=None)  # type: ignore[method-assign]

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
        "name": "Sykkeløkt (3)",
        "duration_seconds": 4859,
        "avg_hr": 120,
        "max_hr": 146,
        "distance_meters": 25874.6,
        "elevation_gain_meters": 120.0,
        "avg_power": 120,
        "max_power": 320,
        "normalized_power": 145,
        "avg_cadence": 88,
        "calories": 612,
        "aerobic_training_effect": 3.4,
        "anaerobic_training_effect": 1.1,
        "training_effect_label": "TEMPO",
        "vo2_max": 52.0,
        "moderate_intensity_minutes": 15,
        "vigorous_intensity_minutes": 25,
        "min_hr": 92,
        "avg_stride_length_cm": 148.0,
        "avg_ground_contact_time_ms": 240,
        "garmin_training_load": 95.2,
        "raw": {"activityId": 42, "extra": "garmin-data"},
    }

    strava_row = MagicMock()
    strava_row.source_id = "s99"
    strava_row.raw = {"id": 99}
    strava_row.garmin_supplement = None
    # Strava-priority fields start with Strava's (less accurate) values; the
    # merge should overwrite them with Garmin's.
    strava_row.name = "Afternoon Ride"
    strava_row.duration_seconds = 5132
    strava_row.avg_hr = 118
    strava_row.max_hr = 146
    strava_row.distance_meters = 25871.0
    strava_row.elevation_gain_meters = 110.0
    strava_row.avg_power = 112
    strava_row.max_power = 310
    strava_row.normalized_power = 138
    strava_row.avg_cadence = 86
    strava_row.calories = 580
    strava_row.garmin_training_load = 80.0  # stale value from an earlier merge
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

    assert merged is strava_row
    assert strava_row.raw == {"id": 99}  # untouched
    assert strava_row.garmin_supplement == garmin_mapped["raw"]
    # Garmin device measurements and the user-set name win
    assert strava_row.name == "Sykkeløkt (3)"
    assert strava_row.duration_seconds == 4859
    assert strava_row.avg_hr == 120
    assert strava_row.distance_meters == 25874.6
    assert strava_row.avg_power == 120
    assert strava_row.normalized_power == 145
    assert strava_row.avg_cadence == 88
    assert strava_row.calories == 612
    assert strava_row.garmin_training_load == 95.2
    # Garmin-only fields fill in (they were None on Strava)
    assert strava_row.aerobic_training_effect == 3.4
    assert strava_row.anaerobic_training_effect == 1.1
    assert strava_row.training_effect_label == "TEMPO"
    assert strava_row.vo2_max == 52.0
    assert strava_row.moderate_intensity_minutes == 15
    assert strava_row.vigorous_intensity_minutes == 25
    assert strava_row.min_hr == 92
    assert strava_row.avg_stride_length_cm == 148.0
    assert strava_row.avg_ground_contact_time_ms == 240


def test_merge_into_strava_skips_garmin_priority_fields_when_null_in_payload() -> None:
    """If Garmin's payload doesn't carry a measurement (e.g. no power meter,
    no user-set name), keep Strava's value rather than nulling it out."""
    ingestor = GarminIngestor(client=MagicMock())
    garmin_mapped = {
        "source": "garmin",
        "source_id": "g42",
        "start_time": datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        "name": None,  # Garmin auto-recorded session with no user label
        "duration_seconds": 4859,
        "avg_hr": 120,
        # power fields intentionally absent from the dict
        "raw": {"activityId": 42},
    }
    strava_row = MagicMock()
    strava_row.raw = {"id": 99}
    strava_row.name = "Afternoon Ride"  # Strava's auto name should stick
    strava_row.duration_seconds = 5132
    strava_row.avg_hr = 118
    strava_row.avg_power = 200  # pre-existing Strava value should stick
    strava_row.normalized_power = 220
    for field in (
        "max_hr",
        "distance_meters",
        "elevation_gain_meters",
        "max_power",
        "avg_cadence",
        "calories",
        "aerobic_training_effect",
        "anaerobic_training_effect",
        "training_effect_label",
        "vo2_max",
        "moderate_intensity_minutes",
        "vigorous_intensity_minutes",
        "min_hr",
        "avg_stride_length_cm",
        "avg_ground_contact_time_ms",
    ):
        setattr(strava_row, field, None)
    session = MagicMock(spec=Session)
    session.scalar.return_value = strava_row

    ingestor._merge_into_strava_if_exists(session, garmin_mapped, MagicMock())

    assert strava_row.name == "Afternoon Ride"  # Garmin had no value → keep Strava's
    assert strava_row.duration_seconds == 4859  # Garmin wins
    assert strava_row.avg_hr == 120  # Garmin wins
    assert strava_row.avg_power == 200  # Garmin had no value → keep Strava's
    assert strava_row.normalized_power == 220


def test_merge_into_strava_returns_none_when_no_match() -> None:
    ingestor = GarminIngestor(client=MagicMock())
    garmin_mapped = {
        "source": "garmin",
        "source_id": "g42",
        "start_time": datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        "raw": {"activityId": 42},
    }
    session = MagicMock(spec=Session)
    session.scalar.return_value = None

    assert ingestor._merge_into_strava_if_exists(session, garmin_mapped, MagicMock()) is None


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
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=None)  # type: ignore[method-assign]

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
    _stub_token_store(monkeypatch, stored=None)
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
    _stub_token_store(monkeypatch, stored=None)

    captured_path: list[str] = []
    fake_client = object()

    def factory(path: str) -> object:
        captured_path.append(path)
        return fake_client

    ingestor = GarminIngestor(client_factory=factory)
    client = ingestor._initialize_client()
    assert client is fake_client
    assert captured_path[0].endswith(".garminconnect")


def test_initialize_client_prefers_stored_tokens_over_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The secret is a seed, not the source of truth — once a token has been
    stored, the secret holds one Garmin has already invalidated."""

    class FakeSettings:
        GARMINTOKENS_B64 = "c2hvdWxkLW5vdC1iZS11c2Vk"

    monkeypatch.setattr("training_pipeline.ingestors.garmin.get_settings", lambda: FakeSettings())
    _stub_token_store(monkeypatch, stored='{"di_refresh_token": "stored"}')

    captured: list[str] = []
    ingestor = GarminIngestor(client_factory=lambda ts: captured.append(ts) or object())
    ingestor._initialize_client()

    assert captured == ['{"di_refresh_token": "stored"}']


def test_sync_persists_rotated_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: Garmin rotates the refresh token on every refresh and kills
    the old one. Without persisting what the client ends up holding, the next
    run replays a dead token and 401s forever."""
    saved = _stub_token_store(monkeypatch, stored='{"di_refresh_token": "old"}')

    rotated = '{"di_refresh_token": "rotated"}'
    ingestor = GarminIngestor(client=_FakeGarminClient(rotated))
    ingestor._sync_activities = MagicMock()  # type: ignore[method-assign]
    ingestor._sync_daily_summaries = MagicMock()  # type: ignore[method-assign]

    ingestor._sync(_make_session(), datetime(2026, 9, 17, tzinfo=UTC))

    assert saved == [rotated]


def test_sync_persists_rotated_tokens_even_when_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotation that happened mid-sync must outlive the failure, or the
    failed run takes the only usable token down with it."""
    saved = _stub_token_store(monkeypatch, stored='{"di_refresh_token": "old"}')

    rotated = '{"di_refresh_token": "rotated"}'
    ingestor = GarminIngestor(client=_FakeGarminClient(rotated))
    ingestor._sync_activities = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )
    ingestor._sync_daily_summaries = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="boom"):
        ingestor._sync(_make_session(), datetime(2026, 9, 17, tzinfo=UTC))

    assert saved == [rotated]


def test_sync_does_not_rewrite_unchanged_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    unchanged = '{"di_refresh_token": "same"}'
    saved = _stub_token_store(monkeypatch, stored=unchanged)

    ingestor = GarminIngestor(client_factory=lambda ts: _FakeGarminClient(unchanged))
    ingestor._sync_activities = MagicMock()  # type: ignore[method-assign]
    ingestor._sync_daily_summaries = MagicMock()  # type: ignore[method-assign]

    ingestor._sync(_make_session(), datetime(2026, 9, 17, tzinfo=UTC))

    assert saved == []


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
    ingestor._merge_into_strava_if_exists = MagicMock(return_value=None)  # type: ignore[method-assign]

    session = _make_session()
    result = IngestionResult()
    ingestor._sync_activities(
        client, session, datetime(2020, 1, 1, tzinfo=UTC), result, MagicMock()
    )

    assert result.records_processed == GARMIN_PAGE_SIZE + 1
    # Should have made exactly 2 pagination calls (second one returns short page → stop)
    assert client.get_activities.call_count == 2


# ---------------------------------------------------------------------------
# Lap / exercise-set detail
#
# The payloads below are trimmed copies of real Garmin responses (activities
# 24436612923, a 4x8 indoor ride, and 24459270339, a strength session), keeping
# the key names and the null-heavy shape the live API actually returns.
# ---------------------------------------------------------------------------


def _splits_response() -> dict[str, Any]:
    def lap(
        index: int,
        intensity: str,
        duration: float,
        avg_power: float,
        max_power: float,
        avg_hr: float,
        cadence: float,
    ) -> dict[str, Any]:
        return {
            "lapIndex": index,
            "messageIndex": index - 1,
            "intensityType": intensity,
            "duration": duration,
            "elapsedDuration": duration,
            "movingDuration": duration,
            "distance": 2813.62,
            "averagePower": avg_power,
            "maxPower": max_power,
            "minPower": 0.0,
            "normalizedPower": avg_power - 2,
            "averageHR": avg_hr,
            "maxHR": avg_hr + 7,
            "averageBikeCadence": cadence,
            "maxBikeCadence": cadence + 8,
            "calories": 150.0,
        }

    return {
        "activityId": 24436612923,
        "lapDTOs": [
            lap(1, "WARMUP", 300.0, 101.0, 103.0, 109.0, 69.0),
            lap(2, "ACTIVE", 480.0, 193.0, 198.0, 149.0, 75.0),
            lap(3, "RECOVERY", 162.155, 81.0, 193.0, 127.0, 60.0),
            lap(4, "COOLDOWN", 232.26, 81.0, 193.0, 122.0, 62.0),
        ],
        "eventDTOs": [],
    }


def _exercise_sets_response() -> dict[str, Any]:
    return {
        "activityId": 24459270339,
        "exerciseSets": [
            {
                "exercises": [
                    {"category": "BENCH_PRESS", "name": None, "probability": 99.609375},
                    {"category": "SHOULDER_PRESS", "name": None, "probability": 79.6875},
                    {"category": "UNKNOWN", "name": None, "probability": 19.921875},
                ],
                "duration": 128.7,
                "repetitionCount": 12,
                "weight": 40000.0,
                "setType": "ACTIVE",
                "startTime": "2026-09-22T16:24:37.0",
                "messageIndex": 0,
                "wktStepIndex": None,
            },
            {
                "exercises": [],
                "duration": 115.37,
                "repetitionCount": None,
                "weight": None,
                "setType": "REST",
                "startTime": "2026-09-22T16:31:49.0",
                "messageIndex": 1,
                "wktStepIndex": None,
            },
            {
                "exercises": [
                    {"category": "PUSH_UP", "name": None, "probability": 59.765625},
                    {
                        "category": "TRICEPS_EXTENSION",
                        "name": "BENCH_DIP",
                        "probability": 89.453125,
                    },
                    {"category": "UNKNOWN", "name": None, "probability": 39.84375},
                ],
                "duration": 32.0,
                "repetitionCount": 13,
                # Garmin's "no load recorded" placeholder, seen on real sets.
                "weight": 0.0,
                "setType": "ACTIVE",
                "startTime": "2026-09-22T16:45:10.0",
                "messageIndex": 2,
                "wktStepIndex": None,
            },
        ],
    }


def test_parse_laps_maps_power_and_intensity() -> None:
    rows = _parse_laps(_splits_response())

    assert [r["lap_index"] for r in rows] == [1, 2, 3, 4]
    assert [r["lap_type"] for r in rows] == ["WARMUP", "ACTIVE", "RECOVERY", "COOLDOWN"]

    work = rows[1]
    assert work["duration_s"] == 480.0
    assert work["avg_power"] == 193
    assert work["max_power"] == 198
    assert work["normalized_power"] == 191
    assert work["avg_hr"] == 149
    assert work["max_hr"] == 156
    # Per-lap cadence lives under averageBikeCadence, not the session-level
    # averageBikingCadenceInRevPerMinute.
    assert work["avg_cadence"] == 75
    assert work["distance_meters"] == pytest.approx(2813.62)


def test_parse_laps_falls_back_to_position_when_lap_index_missing() -> None:
    rows = _parse_laps({"lapDTOs": [{"duration": 60.0}, {"duration": 90.0}]})
    assert [r["lap_index"] for r in rows] == [1, 2]


def test_parse_laps_handles_missing_and_malformed_payloads() -> None:
    assert _parse_laps(None) == []
    assert _parse_laps({}) == []
    assert _parse_laps({"lapDTOs": None}) == []
    assert _parse_laps({"lapDTOs": ["not-a-lap"]}) == []


def test_top_exercise_picks_highest_probability_candidate() -> None:
    name, confidence = _top_exercise(
        [
            {"category": "BENCH_PRESS", "name": None, "probability": 79.6875},
            {"category": "SHOULDER_PRESS", "name": None, "probability": 79.6875},
            {"category": "UNKNOWN", "name": None, "probability": 19.921875},
        ]
    )
    assert name == "BENCH_PRESS"
    assert confidence == pytest.approx(79.6875)


def test_top_exercise_keeps_unknown_when_it_genuinely_leads() -> None:
    """A real shape: the watch is sure it does not recognise the movement.

    Naming the 0%-probability runner-up would invent a movement nobody
    claimed, so UNKNOWN has to survive.
    """
    name, confidence = _top_exercise(
        [
            {"category": "UNKNOWN", "name": None, "probability": 99.609375},
            {"category": "CURL", "name": None, "probability": 0.0},
            {"category": "BENCH_PRESS", "name": None, "probability": 0.0},
        ]
    )
    assert name == "UNKNOWN"
    assert confidence == pytest.approx(99.609375)


def test_top_exercise_breaks_ties_towards_the_named_candidate() -> None:
    name, _ = _top_exercise(
        [
            {"category": "UNKNOWN", "name": None, "probability": 50.0},
            {"category": "PUSH_UP", "name": None, "probability": 50.0},
        ]
    )
    assert name == "PUSH_UP"


def test_top_exercise_includes_sub_category_when_present() -> None:
    name, _ = _top_exercise(
        [{"category": "TRICEPS_EXTENSION", "name": "BENCH_DIP", "probability": 89.5}]
    )
    assert name == "TRICEPS_EXTENSION/BENCH_DIP"


def test_top_exercise_handles_empty_and_malformed() -> None:
    assert _top_exercise([]) == (None, None)
    assert _top_exercise(None) == (None, None)
    assert _top_exercise(["nope"]) == (None, None)


def test_parse_exercise_sets_maps_reps_weight_and_rest() -> None:
    rows = _parse_exercise_sets(_exercise_sets_response())

    assert [r["set_index"] for r in rows] == [0, 1, 2]
    assert [r["set_type"] for r in rows] == ["ACTIVE", "REST", "ACTIVE"]

    first = rows[0]
    assert first["exercise_name"] == "BENCH_PRESS"
    assert first["exercise_confidence"] == pytest.approx(99.609375)
    assert first["reps"] == 12
    assert first["weight_kg"] == pytest.approx(40.0)  # grams on the wire
    assert first["duration_s"] == pytest.approx(128.7)

    # Rest blocks are kept — rest length is half of a readable strength session.
    rest = rows[1]
    assert rest["reps"] is None
    assert rest["exercise_name"] is None
    assert rest["duration_s"] == pytest.approx(115.37)

    # A 0 g weight is Garmin's placeholder, not a real bodyweight load.
    assert rows[2]["weight_kg"] is None
    assert rows[2]["exercise_name"] == "TRICEPS_EXTENSION/BENCH_DIP"


def test_parse_exercise_sets_handles_missing_and_malformed_payloads() -> None:
    assert _parse_exercise_sets(None) == []
    assert _parse_exercise_sets({}) == []
    assert _parse_exercise_sets({"exerciseSets": None}) == []
    assert _parse_exercise_sets({"exerciseSets": ["nope"]}) == []


def _detail_ingestor_and_row() -> tuple[GarminIngestor, MagicMock]:
    ingestor = GarminIngestor(client=MagicMock())
    row = MagicMock()
    row.id = uuid4()
    return ingestor, row


def test_sync_activity_detail_stores_laps_for_interval_ride() -> None:
    ingestor, row = _detail_ingestor_and_row()
    client = MagicMock()
    client.get_activity_splits.return_value = _splits_response()
    session = _make_session()

    ingestor._sync_activity_detail(
        client,
        session,
        {"activityId": 24436612923, "hasIntensityIntervals": True},
        {"source": "garmin", "source_id": "24436612923", "sport_type": "cycling"},
        row,
        MagicMock(),
    )

    client.get_activity_splits.assert_called_once_with("24436612923")
    client.get_activity_exercise_sets.assert_not_called()
    added = session.add_all.call_args[0][0]
    assert len(added) == 4
    assert all(isinstance(lap, ActivityLap) for lap in added)
    assert added[1].avg_power == 193
    assert added[1].activity_id == row.id
    # Replace, not merge: stale laps are cleared first.
    session.execute.assert_called_once()


def test_sync_activity_detail_skips_ride_without_intensity_intervals() -> None:
    ingestor, row = _detail_ingestor_and_row()
    client = MagicMock()
    session = _make_session()

    ingestor._sync_activity_detail(
        client,
        session,
        {"activityId": 1, "hasIntensityIntervals": False},
        {"source": "garmin", "source_id": "1", "sport_type": "cycling"},
        row,
        MagicMock(),
    )

    client.get_activity_splits.assert_not_called()
    session.add_all.assert_not_called()


def test_sync_activity_detail_stores_exercise_sets_for_lifting() -> None:
    ingestor, row = _detail_ingestor_and_row()
    client = MagicMock()
    client.get_activity_exercise_sets.return_value = _exercise_sets_response()
    session = _make_session()

    ingestor._sync_activity_detail(
        client,
        session,
        {"activityId": 24459270339},
        {"source": "garmin", "source_id": "24459270339", "sport_type": "lifting"},
        row,
        MagicMock(),
    )

    client.get_activity_exercise_sets.assert_called_once_with("24459270339")
    client.get_activity_splits.assert_not_called()
    added = session.add_all.call_args[0][0]
    assert len(added) == 3
    assert all(isinstance(s, ActivityExerciseSet) for s in added)
    assert added[0].reps == 12
    assert added[0].weight_kg == pytest.approx(40.0)


def test_sync_activity_detail_survives_a_failing_endpoint() -> None:
    ingestor, row = _detail_ingestor_and_row()
    client = MagicMock()
    client.get_activity_exercise_sets.side_effect = RuntimeError("500 from Garmin")
    session = _make_session()

    ingestor._sync_activity_detail(
        client,
        session,
        {"activityId": 7},
        {"source": "garmin", "source_id": "7", "sport_type": "lifting"},
        row,
        MagicMock(),
    )

    session.add_all.assert_not_called()


def test_sync_activity_detail_skips_sports_without_detail() -> None:
    ingestor, row = _detail_ingestor_and_row()
    client = MagicMock()
    session = _make_session()

    ingestor._sync_activity_detail(
        client,
        session,
        {"activityId": 3, "hasIntensityIntervals": True},
        {"source": "garmin", "source_id": "3", "sport_type": "running"},
        row,
        MagicMock(),
    )

    client.get_activity_splits.assert_not_called()
    client.get_activity_exercise_sets.assert_not_called()


def test_sync_activity_detail_looks_up_row_when_not_merged() -> None:
    ingestor, row = _detail_ingestor_and_row()
    client = MagicMock()
    client.get_activity_exercise_sets.return_value = _exercise_sets_response()
    session = _make_session()
    session.scalar.return_value = row

    ingestor._sync_activity_detail(
        client,
        session,
        {"activityId": 9},
        {"source": "garmin", "source_id": "9", "sport_type": "lifting"},
        None,
        MagicMock(),
    )

    assert session.add_all.call_args[0][0][0].activity_id == row.id


def test_sync_activity_detail_warns_when_row_cannot_be_found() -> None:
    ingestor, _ = _detail_ingestor_and_row()
    client = MagicMock()
    session = _make_session()
    session.scalar.return_value = None
    log = MagicMock()

    ingestor._sync_activity_detail(
        client,
        session,
        {"activityId": 9},
        {"source": "garmin", "source_id": "9", "sport_type": "lifting"},
        None,
        log,
    )

    client.get_activity_exercise_sets.assert_not_called()
    log.warning.assert_called_once()
