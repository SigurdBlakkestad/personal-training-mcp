"""Unit tests for the Open-Meteo weather forecast client."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from training_pipeline.ingestors.http import HttpClient
from training_pipeline.mcp_server.weather import (
    describe_weather_code,
    fetch_forecast,
)


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    transport = httpx.MockTransport(handler)
    return HttpClient(transport=transport)


def _canned_payload() -> dict[str, Any]:
    return {
        "latitude": 59.91,
        "longitude": 10.75,
        "timezone": "Europe/Oslo",
        "daily_units": {},
        "daily": {
            "time": ["2026-05-15", "2026-05-16", "2026-05-17"],
            "temperature_2m_max": [18.5, 14.2, 11.0],
            "temperature_2m_min": [9.0, 7.5, 6.0],
            "precipitation_sum": [0.0, 3.2, 12.4],
            "precipitation_probability_max": [10, 80, 95],
            "wind_speed_10m_max": [12.0, 25.5, 30.0],
            "weathercode": [1, 61, 95],
        },
    }


def test_fetch_forecast_returns_one_row_per_day() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_payload())

    with _make_client(handler) as client:
        result = fetch_forecast(latitude=59.91, longitude=10.75, days=3, client=client)
    assert result["latitude"] == 59.91
    assert result["timezone"] == "Europe/Oslo"
    assert len(result["daily"]) == 3
    day0 = result["daily"][0]
    assert day0["date"] == "2026-05-15"
    assert day0["temperature_max_c"] == 18.5
    assert day0["precipitation_probability_max_pct"] == 10
    assert day0["condition"] == "Mainly clear"


def test_fetch_forecast_request_params() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_canned_payload())

    with _make_client(handler) as client:
        fetch_forecast(
            latitude=60.0,
            longitude=11.0,
            timezone="Europe/Oslo",
            days=7,
            client=client,
        )
    qs = parse_qs(urlparse(captured["url"]).query)
    assert qs["latitude"] == ["60.0"]
    assert qs["longitude"] == ["11.0"]
    assert qs["forecast_days"] == ["7"]
    assert qs["timezone"] == ["Europe/Oslo"]
    daily = qs["daily"][0]
    for var in ("temperature_2m_max", "precipitation_sum", "weathercode"):
        assert var in daily


def test_fetch_forecast_maps_rain_and_thunder_codes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_payload())

    with _make_client(handler) as client:
        result = fetch_forecast(latitude=59.91, longitude=10.75, days=3, client=client)
    conditions = [d["condition"] for d in result["daily"]]
    assert conditions == ["Mainly clear", "Slight rain", "Thunderstorm"]


def test_fetch_forecast_rejects_out_of_range_days() -> None:
    with pytest.raises(ValueError):
        fetch_forecast(latitude=59.91, longitude=10.75, days=0)
    with pytest.raises(ValueError):
        fetch_forecast(latitude=59.91, longitude=10.75, days=17)


def test_fetch_forecast_handles_empty_daily_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"latitude": 59.91, "longitude": 10.75})

    with _make_client(handler) as client:
        result = fetch_forecast(latitude=59.91, longitude=10.75, days=3, client=client)
    assert result["daily"] == []


def test_describe_weather_code_known_and_unknown() -> None:
    assert describe_weather_code(0) == "Clear sky"
    assert describe_weather_code(95) == "Thunderstorm"
    assert describe_weather_code(None) == "Unknown"
    assert describe_weather_code(123).startswith("Unknown")
