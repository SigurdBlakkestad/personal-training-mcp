import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.orm import Session

from training_pipeline.ingestors.http import HttpClient
from training_pipeline.ingestors.strava import (
    STRAVA_OVERLAP_MARGIN_HOURS,
    STRAVA_PAGE_SIZE,
    StravaIngestor,
    _normalize_sport,
)


class FakeSettings:
    STRAVA_CLIENT_ID = "cid"
    STRAVA_CLIENT_SECRET = "csecret"
    STRAVA_REFRESH_TOKEN = "env-refresh"


@pytest.fixture(autouse=True)
def patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("training_pipeline.ingestors.strava.get_settings", lambda: FakeSettings())


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    transport = httpx.MockTransport(handler)
    return HttpClient(
        base_url="https://www.strava.com",
        backoff_min=0.001,
        backoff_max=0.005,
        transport=transport,
    )


def _activity(
    activity_id: int,
    sport: str = "Ride",
    start: str = "2026-04-01T10:00:00Z",
) -> dict[str, Any]:
    return {
        "id": activity_id,
        "name": f"Activity {activity_id}",
        "sport_type": sport,
        "start_date": start,
        "elapsed_time": 3600,
        "moving_time": 3480,
        "distance": 30000.0,
        "total_elevation_gain": 250.0,
        "average_heartrate": 142.0,
        "max_heartrate": 175.0,
        "average_watts": 210.0,
        "weighted_average_watts": 225.0,
        "max_watts": 612,
        "average_cadence": 88.0,
        "calories": 600.0,
        "suffer_score": 87,
        "kilojoules": 750.4,
        "average_speed": 8.62,
        "trainer": False,
        "workout_type": 12,
        "description": "Z2 endurance",
    }


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "access-xyz",
            "refresh_token": "env-refresh",
            "expires_at": 9999999999,
        },
    )


def _rotated_token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "access-new",
            "refresh_token": "rotated-refresh",
            "expires_at": 9999999999,
        },
    )


def _make_session(*, latest_run: Any = None, upsert_inserted: bool = True) -> MagicMock:
    session = MagicMock(spec=Session)
    session.scalar.return_value = latest_run
    session.execute.return_value.scalar_one.return_value = upsert_inserted
    return session


def test_normalize_sport_known_and_unknown() -> None:
    assert _normalize_sport("Ride") == "cycling"
    assert _normalize_sport("MountainBikeRide") == "cycling"
    assert _normalize_sport("Run") == "running"
    assert _normalize_sport("WeightTraining") == "lifting"
    assert _normalize_sport("Walk") == "walking"
    assert _normalize_sport("Hike") == "walking"
    assert _normalize_sport("Swim") == "swimming"
    assert _normalize_sport("Kayaking") == "other"
    assert _normalize_sport(None) == "other"


def test_refresh_token_flow_and_pagination() -> None:
    page_one = [_activity(i) for i in range(STRAVA_PAGE_SIZE)]
    page_two = [_activity(i) for i in range(STRAVA_PAGE_SIZE, STRAVA_PAGE_SIZE + 5)]

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/oauth/token":
            return _token_response()
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(200, json=page_one)
        if page == 2:
            return httpx.Response(200, json=page_two)
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    ingestor = StravaIngestor(http_client=client)
    session = _make_session()

    try:
        result = ingestor._sync(session, since=datetime(2026, 4, 1, tzinfo=UTC))
    finally:
        client.close()

    assert calls[0] == "/oauth/token"
    assert calls.count("/api/v3/athlete/activities") == 2
    assert result.records_processed == STRAVA_PAGE_SIZE + 5
    assert result.records_inserted == STRAVA_PAGE_SIZE + 5
    assert result.cursor is not None
    assert json.loads(result.cursor) == {"refresh_token": "env-refresh"}


def test_sport_type_normalization_applied_to_upsert() -> None:
    received: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        return httpx.Response(
            200,
            json=[
                _activity(1, sport="WeightTraining"),
                _activity(2, sport="Run"),
                _activity(3, sport="Kayaking"),
            ],
        )

    client = _make_client(handler)
    ingestor = StravaIngestor(http_client=client)
    session = _make_session()

    original_upsert = ingestor.upsert_activity

    def capture(s: Any, payload: dict[str, Any]) -> str:
        received.append(payload)
        return original_upsert(s, payload)

    ingestor.upsert_activity = capture  # type: ignore[method-assign]

    try:
        ingestor._sync(session, since=datetime(2026, 4, 1, tzinfo=UTC))
    finally:
        client.close()

    sports = [r["sport_type"] for r in received]
    assert sports == ["lifting", "running", "other"]
    raw_sports = [r["raw"]["sport_type"] for r in received]
    assert raw_sports == ["WeightTraining", "Run", "Kayaking"]


def test_refresh_token_rotation_captured_in_cursor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _rotated_token_response()
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    ingestor = StravaIngestor(http_client=client)
    session = _make_session()

    try:
        result = ingestor._sync(session, since=datetime(2026, 4, 1, tzinfo=UTC))
    finally:
        client.close()

    assert result.cursor is not None
    assert json.loads(result.cursor) == {"refresh_token": "rotated-refresh"}


def test_existing_cursor_refresh_token_used(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_refresh: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            captured_refresh.append(
                dict(httpx.QueryParams(request.read().decode()))["refresh_token"]
            )
            return _token_response()
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    ingestor = StravaIngestor(http_client=client)

    stored_run = MagicMock()
    stored_run.cursor = json.dumps({"refresh_token": "cursor-refresh"})
    session = _make_session(latest_run=stored_run)

    try:
        ingestor._sync(session, since=datetime(2026, 4, 1, tzinfo=UTC))
    finally:
        client.close()

    assert captured_refresh == ["cursor-refresh"]


def test_rate_limit_pause_when_usage_high() -> None:
    pauses: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(
                200,
                json=[_activity(1)] * STRAVA_PAGE_SIZE,
                headers={
                    "X-RateLimit-Usage": "95,500",
                    "X-RateLimit-Limit": "100,1000",
                },
            )
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    ingestor = StravaIngestor(
        http_client=client,
        sleeper=lambda secs: pauses.append(secs),
        now=lambda: 0.0,
    )
    session = _make_session()

    try:
        ingestor._sync(session, since=datetime(2026, 4, 1, tzinfo=UTC))
    finally:
        client.close()

    assert len(pauses) == 1
    assert pauses[0] == 901.0


def test_rate_limit_no_pause_under_threshold() -> None:
    pauses: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        return httpx.Response(
            200,
            json=[_activity(1)],
            headers={
                "X-RateLimit-Usage": "10,100",
                "X-RateLimit-Limit": "100,1000",
            },
        )

    client = _make_client(handler)
    ingestor = StravaIngestor(
        http_client=client,
        sleeper=lambda secs: pauses.append(secs),
        now=lambda: 0.0,
    )
    session = _make_session()

    try:
        ingestor._sync(session, since=datetime(2026, 4, 1, tzinfo=UTC))
    finally:
        client.close()

    assert pauses == []


def test_default_since_365_days_when_no_prior_run() -> None:
    captured_after: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        captured_after.append(int(request.url.params["after"]))
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    ingestor = StravaIngestor(http_client=client)
    session = _make_session(latest_run=None)

    before = datetime.now(UTC) - timedelta(days=365)
    try:
        ingestor._sync(session, since=None)
    finally:
        client.close()
    after = datetime.now(UTC) - timedelta(days=365)

    assert len(captured_after) == 1
    assert int(before.timestamp()) - 5 <= captured_after[0] <= int(after.timestamp()) + 5


def test_activity_mapping_handles_missing_optional_fields() -> None:
    minimal = {
        "id": 999,
        "name": "Mystery",
        "sport_type": "Run",
        "start_date": "2026-04-02T07:30:00Z",
        "elapsed_time": 1800,
        "distance": 5000.0,
    }
    ingestor = StravaIngestor(http_client=MagicMock(spec=HttpClient))
    mapped = ingestor._map_activity(minimal)
    assert mapped["source"] == "strava"
    assert mapped["source_id"] == "999"
    assert mapped["sport_type"] == "running"
    assert mapped["start_time"] == datetime(2026, 4, 2, 7, 30, tzinfo=UTC)
    assert mapped["end_time"] == datetime(2026, 4, 2, 8, 0, tzinfo=UTC)
    assert mapped["avg_hr"] is None
    assert mapped["max_hr"] is None
    assert mapped["avg_power"] is None
    assert mapped["normalized_power"] is None
    assert mapped["calories"] is None
    assert mapped["moving_time_seconds"] is None
    assert mapped["suffer_score"] is None
    assert mapped["kilojoules"] is None
    assert mapped["average_speed_ms"] is None
    assert mapped["is_trainer"] is None
    assert mapped["workout_type"] is None
    assert mapped["description"] is None
    assert mapped["max_power"] is None
    assert mapped["raw"] is minimal


def test_activity_mapping_extracts_rich_strava_fields() -> None:
    ingestor = StravaIngestor(http_client=MagicMock(spec=HttpClient))
    mapped = ingestor._map_activity(_activity(42))
    assert mapped["moving_time_seconds"] == 3480
    assert mapped["suffer_score"] == 87
    assert mapped["kilojoules"] == 750.4
    assert mapped["average_speed_ms"] == 8.62
    assert mapped["is_trainer"] is False
    assert mapped["workout_type"] == 12
    assert mapped["description"] == "Z2 endurance"
    assert mapped["max_power"] == 612


def test_consume_garmin_row_repurposes_existing_row() -> None:
    ingestor = StravaIngestor(http_client=MagicMock(spec=HttpClient))
    start_time = datetime(2026, 4, 1, 10, 0, 30, tzinfo=UTC)
    mapped = {
        "source": "strava",
        "source_id": "s99",
        "start_time": start_time,
        "sport_type": "cycling",
        "name": "Afternoon Ride",
        "duration_seconds": 5132,
        "avg_hr": 118,
        "max_hr": 146,
        "distance_meters": 25871.0,
        "avg_power": 112,
        "raw": {"id": 99, "elapsed_time": 3600},
    }
    garmin_row = MagicMock()
    garmin_row.raw = {"activityId": 42, "trainingEffectLabel": "TEMPO"}
    # Garmin-set name and device measurements that must survive the consume
    garmin_row.name = "Sykkeløkt (3)"
    garmin_row.duration_seconds = 4859
    garmin_row.avg_hr = 120
    garmin_row.max_hr = 146
    garmin_row.distance_meters = 25874.6
    garmin_row.avg_power = 120
    garmin_row.elevation_gain_meters = None
    garmin_row.max_power = None
    garmin_row.normalized_power = None
    garmin_row.avg_cadence = None
    garmin_row.calories = None
    garmin_row.aerobic_training_effect = 3.4
    garmin_row.min_hr = 92

    session = MagicMock(spec=Session)
    # First scalar() call returns the garmin row, second returns None (no conflicting strava row)
    session.scalar.side_effect = [garmin_row, None]

    consumed = ingestor._consume_garmin_row_if_exists(session, mapped, MagicMock())
    assert consumed is True
    assert garmin_row.source == "strava"
    assert garmin_row.source_id == "s99"
    # Garmin-set name survives — Strava's auto-generated label is discarded
    assert garmin_row.name == "Sykkeløkt (3)"
    # Garmin device measurements win, even though Strava's mapped dict had values
    assert garmin_row.duration_seconds == 4859
    assert garmin_row.avg_hr == 120
    assert garmin_row.distance_meters == 25874.6
    assert garmin_row.avg_power == 120
    # Garmin-only columns survive — Strava's mapped dict doesn't include them
    assert garmin_row.aerobic_training_effect == 3.4
    assert garmin_row.min_hr == 92
    # Raw is now Strava's payload; the prior Garmin raw lives on garmin_supplement
    assert garmin_row.raw == {"id": 99, "elapsed_time": 3600}
    assert garmin_row.garmin_supplement == {
        "activityId": 42,
        "trainingEffectLabel": "TEMPO",
    }


def test_consume_garmin_row_lets_strava_fill_null_priority_fields() -> None:
    """When Garmin didn't record a particular measurement (column is None on
    the existing row), Strava's value should fill it in. Also covers the
    common case where Garmin has no user-set name."""
    ingestor = StravaIngestor(http_client=MagicMock(spec=HttpClient))
    mapped = {
        "source": "strava",
        "source_id": "s99",
        "start_time": datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        "name": "Afternoon Ride",
        "duration_seconds": 5132,
        "avg_hr": 118,
        "avg_power": 112,  # Strava has power; Garmin didn't record it
        "raw": {"id": 99},
    }
    garmin_row = MagicMock()
    garmin_row.raw = {"activityId": 42}
    garmin_row.name = None  # no user label on the device
    garmin_row.duration_seconds = 4859
    garmin_row.avg_hr = 120
    garmin_row.avg_power = None  # no power meter on this ride
    for field in (
        "max_hr",
        "distance_meters",
        "elevation_gain_meters",
        "max_power",
        "normalized_power",
        "avg_cadence",
        "calories",
    ):
        setattr(garmin_row, field, None)

    session = MagicMock(spec=Session)
    session.scalar.side_effect = [garmin_row, None]

    consumed = ingestor._consume_garmin_row_if_exists(session, mapped, MagicMock())
    assert consumed is True
    assert garmin_row.name == "Afternoon Ride"  # Garmin was None → Strava fills in
    assert garmin_row.duration_seconds == 4859  # Garmin wins
    assert garmin_row.avg_hr == 120  # Garmin wins
    assert garmin_row.avg_power == 112  # Garmin was None → Strava fills in


def test_consume_garmin_row_skipped_when_no_match() -> None:
    ingestor = StravaIngestor(http_client=MagicMock(spec=HttpClient))
    mapped = {
        "source": "strava",
        "source_id": "s99",
        "start_time": datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        "raw": {"id": 99},
    }
    session = MagicMock(spec=Session)
    session.scalar.return_value = None
    assert ingestor._consume_garmin_row_if_exists(session, mapped, MagicMock()) is False


def test_consume_garmin_row_skipped_when_strava_already_exists() -> None:
    ingestor = StravaIngestor(http_client=MagicMock(spec=HttpClient))
    mapped = {
        "source": "strava",
        "source_id": "s99",
        "start_time": datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        "raw": {"id": 99},
    }
    garmin_row = MagicMock()
    session = MagicMock(spec=Session)
    # garmin row found, then strava row found — conflict
    session.scalar.side_effect = [garmin_row, "existing-strava-uuid"]
    assert ingestor._consume_garmin_row_if_exists(session, mapped, MagicMock()) is False


def test_compute_since_subtracts_overlap_margin_from_prior_run() -> None:
    ingestor = StravaIngestor(http_client=MagicMock(spec=HttpClient))
    last_finished = datetime(2026, 6, 7, 8, 18, tzinfo=UTC)
    session = MagicMock(spec=Session)
    session.scalar.return_value = last_finished

    since = ingestor._compute_since(session)

    assert since == last_finished - timedelta(hours=STRAVA_OVERLAP_MARGIN_HOURS)


def test_compute_since_falls_back_to_default_lookback_without_prior_run() -> None:
    ingestor = StravaIngestor(http_client=MagicMock(spec=HttpClient))
    session = MagicMock(spec=Session)
    session.scalar.return_value = None

    before = datetime.now(UTC) - timedelta(days=365)
    since = ingestor._compute_since(session)
    after = datetime.now(UTC) - timedelta(days=365)

    assert before - timedelta(seconds=5) <= since <= after + timedelta(seconds=5)


def test_before_param_bounds_request_window() -> None:
    captured_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        captured_params.append(dict(request.url.params))
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    before = datetime(2026, 6, 10, tzinfo=UTC)
    ingestor = StravaIngestor(http_client=client, before=before)
    session = _make_session()

    try:
        ingestor._sync(session, since=datetime(2026, 6, 1, tzinfo=UTC))
    finally:
        client.close()

    assert len(captured_params) == 1
    assert captured_params[0]["before"] == str(int(before.timestamp()))
    assert captured_params[0]["after"] == str(int(datetime(2026, 6, 1, tzinfo=UTC).timestamp()))


def test_before_param_omitted_by_default() -> None:
    captured_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        captured_params.append(dict(request.url.params))
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    ingestor = StravaIngestor(http_client=client)
    session = _make_session()

    try:
        ingestor._sync(session, since=datetime(2026, 6, 1, tzinfo=UTC))
    finally:
        client.close()

    assert len(captured_params) == 1
    assert "before" not in captured_params[0]
