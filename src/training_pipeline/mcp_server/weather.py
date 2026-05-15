"""Open-Meteo forecast client.

Pulls a multi-day daily forecast for the configured training location so the
MCP coach can decide between indoor and outdoor sessions when planning.
No API key needed — Open-Meteo is free for non-commercial use.
"""

from __future__ import annotations

from typing import Any

from training_pipeline.ingestors.http import HttpClient
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.logging import get_logger

logger = get_logger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_VARIABLES = (
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "precipitation_probability_max",
    "wind_speed_10m_max",
    "weathercode",
)
MIN_FORECAST_DAYS = 1
MAX_FORECAST_DAYS = 16

# WMO weather codes — https://open-meteo.com/en/docs (see weathercode).
_WEATHER_CODE_DESCRIPTIONS: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def describe_weather_code(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return _WEATHER_CODE_DESCRIPTIONS.get(code, f"Unknown (code {code})")


def _parse_daily(payload: dict[str, Any]) -> list[dict[str, Any]]:
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return []
    dates = daily.get("time", [])
    if not isinstance(dates, list):
        return []
    rows: list[dict[str, Any]] = []
    for idx, date_str in enumerate(dates):
        if not isinstance(date_str, str):
            continue

        def _at(field: str, i: int = idx) -> Any:
            values = daily.get(field)
            if isinstance(values, list) and i < len(values):
                return values[i]
            return None

        weather_code = _at("weathercode")
        rows.append(
            {
                "date": date_str,
                "temperature_max_c": _at("temperature_2m_max"),
                "temperature_min_c": _at("temperature_2m_min"),
                "precipitation_sum_mm": _at("precipitation_sum"),
                "precipitation_probability_max_pct": _at("precipitation_probability_max"),
                "wind_speed_max_kmh": _at("wind_speed_10m_max"),
                "weather_code": weather_code,
                "condition": describe_weather_code(
                    int(weather_code) if isinstance(weather_code, int | float) else None
                ),
            }
        )
    return rows


def fetch_forecast(
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    timezone: str | None = None,
    days: int = 7,
    client: HttpClient | None = None,
) -> dict[str, Any]:
    """Return a daily forecast as a dict with ``location`` and ``daily`` rows."""
    if days < MIN_FORECAST_DAYS or days > MAX_FORECAST_DAYS:
        raise ValueError(
            f"days must be between {MIN_FORECAST_DAYS} and {MAX_FORECAST_DAYS} (got {days})"
        )

    settings = get_settings()
    lat = latitude if latitude is not None else settings.WEATHER_LATITUDE
    lon = longitude if longitude is not None else settings.WEATHER_LONGITUDE
    tz = timezone if timezone is not None else settings.WEATHER_TIMEZONE

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(DAILY_VARIABLES),
        "timezone": tz,
        "forecast_days": days,
    }

    owned_client = client is None
    http = client or HttpClient(timeout=15.0)
    try:
        response = http.get(FORECAST_URL, params=params)
    finally:
        if owned_client:
            http.close()

    payload = response.json()
    rows = _parse_daily(payload)
    logger.info(
        "weather.forecast",
        latitude=lat,
        longitude=lon,
        timezone=tz,
        days=days,
        rows=len(rows),
    )
    return {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "daily": rows,
    }
