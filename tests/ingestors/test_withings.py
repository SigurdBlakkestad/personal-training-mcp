import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from training_pipeline.ingestors import withings
from training_pipeline.ingestors.base import IngestionResult
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


@pytest.fixture(autouse=True)
def credential_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """In-memory stand-in for service_credentials; keeps every test off the DB."""
    store: dict[str, str] = {}
    monkeypatch.setattr("training_pipeline.ingestors.withings.load_service_credential", store.get)
    monkeypatch.setattr(
        "training_pipeline.ingestors.withings.save_service_credential", store.__setitem__
    )
    return store


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


def _empty_handler(
    refresh: str = "rotated-refresh",
    captured_refresh: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/oauth2":
            if captured_refresh is not None:
                form = dict(httpx.QueryParams(request.read().decode()))
                captured_refresh.append(form["refresh_token"])
            return httpx.Response(200, json=_envelope(_token_body(refresh)))
        if request.url.path == "/measure":
            return httpx.Response(200, json=_envelope({"measuregrps": []}))
        if request.url.path == "/v2/measure":
            return httpx.Response(200, json=_envelope({"activities": []}))
        if request.url.path == "/v2/sleep":
            return httpx.Response(200, json=_envelope({"series": []}))
        raise AssertionError(f"unexpected path {request.url.path}")

    return handler


def _run_sync(
    handler: Callable[[httpx.Request], httpx.Response],
    session: MagicMock | None = None,
    since: datetime = datetime(2026, 4, 15, tzinfo=UTC),
) -> IngestionResult:
    client = _make_client(handler)
    ingestor = WithingsIngestor(http_client=client)
    try:
        return ingestor._sync(session if session is not None else _make_session(), since=since)
    finally:
        client.close()


def test_rotated_refresh_token_saved_to_service_credentials(
    credential_store: dict[str, str],
) -> None:
    result = _run_sync(_empty_handler("rotated-refresh"))

    assert credential_store == {"withings": "rotated-refresh"}
    # The token no longer rides on the run's cursor.
    assert result.cursor is None


def test_rotated_refresh_token_survives_failed_sync(
    credential_store: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/oauth2":
            return httpx.Response(200, json=_envelope(_token_body("rotated-refresh")))
        if request.url.path == "/measure":
            return httpx.Response(200, json={"status": 503, "error": "try later"})
        raise AssertionError(f"unexpected path {request.url.path}")

    with pytest.raises(WithingsAPIError):
        _run_sync(handler)

    assert credential_store == {"withings": "rotated-refresh"}


def test_stored_refresh_token_preferred_over_secret(
    credential_store: dict[str, str],
) -> None:
    credential_store["withings"] = "stored-refresh"
    captured: list[str] = []

    _run_sync(_empty_handler("next-refresh", captured))

    assert captured == ["stored-refresh"]
    assert credential_store == {"withings": "next-refresh"}


def test_secret_seeds_refresh_token_when_no_row_and_no_cursor(
    credential_store: dict[str, str],
) -> None:
    captured: list[str] = []

    _run_sync(_empty_handler("next-refresh", captured))

    assert captured == [FakeSettings.WITHINGS_REFRESH_TOKEN]
    assert credential_store == {"withings": "next-refresh"}


def test_legacy_cursor_token_used_and_saved_when_no_stored_row(
    credential_store: dict[str, str],
) -> None:
    captured: list[str] = []
    legacy_run = MagicMock()
    legacy_run.cursor = json.dumps({"refresh_token": "cursor-refresh"})
    session = _make_session()
    session.scalar.return_value = legacy_run

    _run_sync(_empty_handler("next-refresh", captured), session=session)

    assert captured == ["cursor-refresh"]
    assert credential_store == {"withings": "next-refresh"}


def test_stored_row_preferred_over_legacy_cursor(
    credential_store: dict[str, str],
) -> None:
    credential_store["withings"] = "stored-refresh"
    captured: list[str] = []
    legacy_run = MagicMock()
    legacy_run.cursor = json.dumps({"refresh_token": "cursor-refresh"})
    session = _make_session()
    session.scalar.return_value = legacy_run

    _run_sync(_empty_handler("next-refresh", captured), session=session)

    assert captured == ["stored-refresh"]


def test_refresh_not_retried_after_read_timeout(
    credential_store: dict[str, str],
) -> None:
    # The request may have reached Withings and rotated the token; a replay
    # would send the invalidated token and lose the rotated one.
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/v2/oauth2":
            attempts += 1
            raise httpx.ReadTimeout("response lost", request=request)
        raise AssertionError(f"unexpected path {request.url.path}")

    with pytest.raises(httpx.ReadTimeout):
        _run_sync(handler)

    assert attempts == 1
    assert credential_store == {}


def test_refresh_not_retried_after_5xx() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/v2/oauth2":
            attempts += 1
            return httpx.Response(502)
        raise AssertionError(f"unexpected path {request.url.path}")

    with pytest.raises(httpx.HTTPStatusError):
        _run_sync(handler)

    assert attempts == 1


def test_refresh_retried_when_connection_never_opened() -> None:
    attempts = 0
    base = _empty_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/v2/oauth2":
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("refused", request=request)
        return base(request)

    _run_sync(handler)

    assert attempts == 2


def test_data_calls_still_retry_read_timeouts() -> None:
    measure_attempts = 0
    base = _empty_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal measure_attempts
        if request.url.path == "/measure":
            measure_attempts += 1
            if measure_attempts == 1:
                raise httpx.ReadTimeout("slow", request=request)
        return base(request)

    _run_sync(handler)

    assert measure_attempts == 2


def _flaky_save(failures: int, store: dict[str, str]) -> Callable[[str, str], None]:
    calls = 0

    def save(service: str, payload: str) -> None:
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise OperationalError("upsert", {}, Exception("db unavailable"))
        store[service] = payload

    return save


def test_refresh_token_save_retried_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, str] = {}
    monkeypatch.setattr(withings, "WITHINGS_CREDENTIAL_SAVE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(withings, "save_service_credential", _flaky_save(2, store))

    _run_sync(_empty_handler("rotated-refresh"))

    assert store == {"withings": "rotated-refresh"}


def test_refresh_token_save_failure_logged_and_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, str] = {}
    monkeypatch.setattr(withings, "WITHINGS_CREDENTIAL_SAVE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(withings, "save_service_credential", _flaky_save(3, store))

    with capture_logs() as logs, pytest.raises(OperationalError):
        _run_sync(_empty_handler("rotated-refresh"))

    assert store == {}
    failures = [e for e in logs if e["event"] == "withings.refresh_token.save_failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "error"
    assert failures[0]["attempts"] == 3
    assert "withings_auth.py" in failures[0]["message"]
    assert all("rotated-refresh" not in repr(entry) for entry in logs)


def _weigh_in(grpid: int, when: datetime, grams: int) -> dict[str, Any]:
    return {
        "grpid": grpid,
        "date": int(when.timestamp()),
        "measures": [{"value": grams, "type": 1, "unit": -3}],
    }


def test_body_measurements_fetched_by_lastupdate() -> None:
    forms: list[dict[str, str]] = []
    since = datetime(2026, 4, 15, 5, 32, tzinfo=UTC)
    base = _empty_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/measure":
            forms.append(dict(httpx.QueryParams(request.read().decode())))
        return base(request)

    _run_sync(handler, since=since)

    assert len(forms) == 1
    assert forms[0]["action"] == "getmeas"
    assert forms[0]["category"] == "1"
    assert forms[0]["lastupdate"] == str(int(since.timestamp()))
    assert "startdate" not in forms[0]
    assert "enddate" not in forms[0]


def test_body_measurements_follow_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    forms: list[dict[str, str]] = []
    first = _weigh_in(1, datetime(2026, 4, 14, 5, 0, tzinfo=UTC), 80500)
    second = _weigh_in(2, datetime(2026, 4, 15, 5, 0, tzinfo=UTC), 80100)
    base = _empty_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/measure":
            return base(request)
        form = dict(httpx.QueryParams(request.read().decode()))
        forms.append(form)
        if "offset" not in form:
            page = {"measuregrps": [first], "more": 1, "offset": 1}
        else:
            page = {"measuregrps": [second], "more": 0, "offset": 0}
        return httpx.Response(200, json=_envelope(page))

    received: list[dict[str, Any]] = []
    client = _make_client(handler)
    ingestor = WithingsIngestor(http_client=client)
    original = ingestor.upsert_body_measurement

    def capture(s: Any, payload: dict[str, Any]) -> str:
        received.append(payload)
        return original(s, payload)

    monkeypatch.setattr(ingestor, "upsert_body_measurement", capture)
    try:
        result = ingestor._sync(_make_session(), since=datetime(2026, 4, 13, tzinfo=UTC))
    finally:
        client.close()

    assert [form.get("offset") for form in forms] == [None, "1"]
    assert forms[1]["lastupdate"] == forms[0]["lastupdate"]
    assert [row["weight_kg"] for row in received] == [
        pytest.approx(80.5),
        pytest.approx(80.1),
    ]
    assert result.records_processed == 2


def test_pagination_without_a_new_offset_raises() -> None:
    base = _empty_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/measure":
            return httpx.Response(200, json=_envelope({"measuregrps": [], "more": 1}))
        return base(request)

    with pytest.raises(WithingsAPIError):
        _run_sync(handler)


def test_default_since_is_last_run_start_minus_overlap() -> None:
    session = _make_session()
    started = datetime(2026, 4, 15, 5, 30, tzinfo=UTC)
    session.scalar.return_value = started

    since = WithingsIngestor(http_client=MagicMock())._compute_since(session)

    assert since == started - timedelta(hours=1)
    stmt = session.scalar.call_args.args[0]
    assert [col.name for col in stmt.selected_columns] == ["started_at"]


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
