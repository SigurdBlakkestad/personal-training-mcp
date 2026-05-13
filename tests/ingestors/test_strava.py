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
        "distance": 30000.0,
        "total_elevation_gain": 250.0,
        "average_heartrate": 142.0,
        "max_heartrate": 175.0,
        "average_watts": 210.0,
        "weighted_average_watts": 225.0,
        "average_cadence": 88.0,
        "calories": 600.0,
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


def test_default_since_30_days_when_no_prior_run() -> None:
    captured_after: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return _token_response()
        captured_after.append(int(request.url.params["after"]))
        return httpx.Response(200, json=[])

    client = _make_client(handler)
    ingestor = StravaIngestor(http_client=client)
    session = _make_session(latest_run=None)

    before = datetime.now(UTC) - timedelta(days=30)
    try:
        ingestor._sync(session, since=None)
    finally:
        client.close()
    after = datetime.now(UTC) - timedelta(days=30)

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
    assert mapped["raw"] is minimal
