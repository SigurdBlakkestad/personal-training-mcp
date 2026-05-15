import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.orm import Session

from training_pipeline.ingestors.http import HttpClient
from training_pipeline.ingestors.withings import (
    WithingsAPIError,
    WithingsIngestor,
    _map_daily,
)


class FakeSettings:
    WITHINGS_CLIENT_ID = "cid"
    WITHINGS_CLIENT_SECRET = "csecret"
    WITHINGS_REFRESH_TOKEN = "env-refresh"


@pytest.fixture(autouse=True)
def patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("training_pipeline.ingestors.withings.get_settings", lambda: FakeSettings())


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    transport = httpx.MockTransport(handler)
    return HttpClient(
        base_url="https://wbsapi.withings.net",
        backoff_min=0.001,
        backoff_max=0.005,
        transport=transport,
    )


def _make_session() -> MagicMock:
    session = MagicMock(spec=Session)
    session.scalar.return_value = None
    session.execute.return_value.scalar_one.return_value = True
    return session


def _envelope(body: dict[str, Any], status: int = 0) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "body": body}
    return payload


def _token_body(refresh: str = "rotated-refresh") -> dict[str, Any]:
    return {
        "access_token": "access-xyz",
        "refresh_token": refresh,
        "userid": "38702996",
        "expires_in": 10800,
    }


def test_refresh_rotation_warns_and_persists_in_cursor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/oauth2":
            return httpx.Response(200, json=_envelope(_token_body("rotated-refresh")))
        if request.url.path == "/measure":
            return httpx.Response(200, json=_envelope({"measuregrps": []}))
        if request.url.path == "/v2/measure":
            return httpx.Response(200, json=_envelope({"activities": []}))
        if request.url.path == "/v2/sleep":
            return httpx.Response(200, json=_envelope({"series": []}))
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _make_client(handler)
    ingestor = WithingsIngestor(http_client=client)
    session = _make_session()

    try:
        result = ingestor._sync(session, since=datetime(2026, 4, 15, tzinfo=UTC))
    finally:
        client.close()

    assert result.cursor is not None
    assert json.loads(result.cursor) == {"refresh_token": "rotated-refresh"}


def test_existing_cursor_refresh_token_used() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/oauth2":
            form = dict(httpx.QueryParams(request.read().decode()))
            captured.append(form["refresh_token"])
            return httpx.Response(200, json=_envelope(_token_body("cursor-refresh")))
        if request.url.path == "/measure":
            return httpx.Response(200, json=_envelope({"measuregrps": []}))
        if request.url.path == "/v2/measure":
            return httpx.Response(200, json=_envelope({"activities": []}))
        if request.url.path == "/v2/sleep":
            return httpx.Response(200, json=_envelope({"series": []}))
        raise AssertionError(f"unexpected path {request.url.path}")

    stored_run = MagicMock()
    stored_run.cursor = json.dumps({"refresh_token": "cursor-refresh"})
    session = MagicMock(spec=Session)
    session.scalar.return_value = stored_run
    session.execute.return_value.scalar_one.return_value = True

    client = _make_client(handler)
    ingestor = WithingsIngestor(http_client=client)

    try:
        ingestor._sync(session, since=datetime(2026, 4, 15, tzinfo=UTC))
    finally:
        client.close()

    assert captured == ["cursor-refresh"]


def test_status_non_zero_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/oauth2":
            return httpx.Response(200, json={"status": 401, "error": "invalid token"})
        raise AssertionError("should not reach further calls")

    client = _make_client(handler)
    ingestor = WithingsIngestor(http_client=client)
    session = _make_session()

    with pytest.raises(WithingsAPIError) as excinfo:
        try:
            ingestor._sync(session, since=datetime(2026, 4, 15, tzinfo=UTC))
        finally:
            client.close()
    assert "401" in str(excinfo.value)


def test_body_measurements_value_unit_conversion_and_type_mapping() -> None:
    received: list[dict[str, Any]] = []

    measure_group = {
        "grpid": 1,
        "date": int(datetime(2026, 4, 14, 7, 30, tzinfo=UTC).timestamp()),
        "measures": [
            {"value": 80500, "type": 1, "unit": -3},  # weight 80.5 kg
            {"value": 175, "type": 6, "unit": -1},  # body_fat 17.5 %
            {"value": 380, "type": 76, "unit": -1},  # muscle 38.0 kg
            {"value": 530, "type": 77, "unit": -1},  # water 53.0 %
            {"value": 32, "type": 88, "unit": -1},  # bone 3.2 kg
            {"value": 12, "type": 5, "unit": 0},  # fat-free mass — not mapped to a column
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/oauth2":
            return httpx.Response(200, json=_envelope(_token_body("env-refresh")))
        if request.url.path == "/measure":
            return httpx.Response(200, json=_envelope({"measuregrps": [measure_group]}))
        if request.url.path == "/v2/measure":
            return httpx.Response(200, json=_envelope({"activities": []}))
        if request.url.path == "/v2/sleep":
            return httpx.Response(200, json=_envelope({"series": []}))
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _make_client(handler)
    ingestor = WithingsIngestor(http_client=client)
    session = _make_session()

    original = ingestor.upsert_body_measurement

    def capture(s: Any, payload: dict[str, Any]) -> str:
        received.append(payload)
        return original(s, payload)

    ingestor.upsert_body_measurement = capture  # type: ignore[method-assign]

    try:
        result = ingestor._sync(session, since=datetime(2026, 4, 14, tzinfo=UTC))
    finally:
        client.close()

    assert len(received) == 1
    payload = received[0]
    assert payload["source"] == "withings"
    assert payload["measured_at"] == datetime(2026, 4, 14, 7, 30, tzinfo=UTC)
    assert payload["weight_kg"] == pytest.approx(80.5)
    assert payload["body_fat_pct"] == pytest.approx(17.5)
    assert payload["muscle_mass_kg"] == pytest.approx(38.0)
    assert payload["water_pct"] == pytest.approx(53.0)
    assert payload["bone_mass_kg"] == pytest.approx(3.2)
    assert "fat_free_mass_kg" not in payload
    assert result.records_processed == 1
    assert result.records_inserted == 1


def test_daily_summary_merges_activity_and_sleep_per_date() -> None:
    captured: list[dict[str, Any]] = []

    activities = [
        {"date": "2026-04-12", "steps": 8123},
        {"date": "2026-04-13", "steps": 5000},
    ]
    sleep_series = [
        {
            "date": "2026-04-13",
            "data": {
                "sleep_score": 82,
                "lightsleepduration": 14400,
                "deepsleepduration": 5400,
                "remsleepduration": 3600,
                "wakeupduration": 600,
            },
        },
        {
            "date": "2026-04-14",
            "data": {
                "sleep_score": 70,
                "lightsleepduration": 10000,
                "deepsleepduration": 4000,
                "remsleepduration": 2000,
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/oauth2":
            return httpx.Response(200, json=_envelope(_token_body("env-refresh")))
        if request.url.path == "/measure":
            return httpx.Response(200, json=_envelope({"measuregrps": []}))
        if request.url.path == "/v2/measure":
            return httpx.Response(200, json=_envelope({"activities": activities}))
        if request.url.path == "/v2/sleep":
            return httpx.Response(200, json=_envelope({"series": sleep_series}))
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _make_client(handler)
    ingestor = WithingsIngestor(http_client=client)
    session = _make_session()

    original = ingestor.upsert_daily_summary

    def capture(s: Any, payload: dict[str, Any]) -> str:
        captured.append(payload)
        return original(s, payload)

    ingestor.upsert_daily_summary = capture  # type: ignore[method-assign]

    try:
        ingestor._sync(session, since=datetime(2026, 4, 10, tzinfo=UTC))
    finally:
        client.close()

    by_date = {row["date"]: row for row in captured}
    assert set(by_date) == {date(2026, 4, 12), date(2026, 4, 13), date(2026, 4, 14)}

    # 2026-04-12: only activity, no sleep
    only_activity = by_date[date(2026, 4, 12)]
    assert only_activity["steps"] == 8123
    assert only_activity["sleep_score"] is None
    assert only_activity["sleep_duration_seconds"] is None
    assert "activity" in only_activity["raw"]
    assert "sleep" not in only_activity["raw"]

    # 2026-04-13: both
    both = by_date[date(2026, 4, 13)]
    assert both["steps"] == 5000
    assert both["sleep_score"] == 82
    assert both["sleep_duration_seconds"] == 14400 + 5400 + 3600
    assert both["raw"]["activity"]["steps"] == 5000
    assert both["raw"]["sleep"]["data"]["wakeupduration"] == 600

    # 2026-04-14: only sleep
    only_sleep = by_date[date(2026, 4, 14)]
    assert only_sleep["steps"] is None
    assert only_sleep["sleep_score"] == 70
    assert only_sleep["sleep_duration_seconds"] == 10000 + 4000 + 2000


def test_map_daily_handles_missing_sleep_data() -> None:
    row = _map_daily(date(2026, 4, 13), {"date": "2026-04-13", "steps": 1234}, None)
    assert row["steps"] == 1234
    assert row["sleep_score"] is None
    assert row["sleep_duration_seconds"] is None
    assert row["raw"] == {"activity": {"date": "2026-04-13", "steps": 1234}}


def test_map_daily_handles_zero_sleep_durations() -> None:
    sleep = {
        "date": "2026-04-13",
        "data": {
            "sleep_score": 0,
            "lightsleepduration": 0,
            "deepsleepduration": 0,
            "remsleepduration": 0,
        },
    }
    row = _map_daily(date(2026, 4, 13), None, sleep)
    assert row["sleep_score"] == 0
    assert row["sleep_duration_seconds"] is None
