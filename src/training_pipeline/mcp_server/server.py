"""FastMCP server exposing coaching tools to Claude.

Tool implementations live in `tools.py`. This module registers them with
FastMCP so they are discoverable over the MCP protocol.
"""

from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from training_pipeline.mcp_server import tools
from training_pipeline.mcp_server.auth import build_auth
from training_pipeline.shared.config import get_settings

_settings = get_settings()
_auth = build_auth(_settings)

mcp: FastMCP[Any] = FastMCP("personal-training", auth=_auth)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.tool
def get_recent_activities(days: int = 14, sport_type: str | None = None) -> list[dict[str, Any]]:
    """Return recent activities with linked RPE/pain from the latest manual log.

    Each row includes Strava and Garmin-derived coaching metrics when available:
    Strava — ``moving_time_min`` (preferred over ``duration_min`` for load
    analysis since it excludes stops), ``suffer_score`` (Strava's relative
    effort), ``kilojoules`` (total work, cycling), ``avg_speed_kmh``,
    ``is_trainer`` (indoor flag), ``workout_type`` (race/workout/default
    code), ``description``, ``max_power`` (peak watts).
    Garmin — ``training_load`` (our TSS proxy), ``aerobic_training_effect``
    and ``anaerobic_training_effect`` (0–5), ``training_effect_label``
    (AEROBIC_BASE/TEMPO/THRESHOLD/VO2MAX/etc.), ``vo2_max``,
    ``moderate_intensity_minutes`` / ``vigorous_intensity_minutes``,
    running dynamics (``avg_stride_length_cm``, ``avg_ground_contact_time_ms``).
    """
    return tools.get_recent_activities(days=days, sport_type=sport_type)


@mcp.tool
def get_activity_by_id(activity_id: str) -> dict[str, Any] | None:
    """Return a single activity by UUID with the linked manual log (if any)."""
    return tools.get_activity_by_id(activity_id)


@mcp.tool
def get_daily_summary(start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Return per-day metrics merged from body_measurements and daily_summary.

    Dates are ISO-8601 (YYYY-MM-DD) and inclusive on both ends.

    Each row exposes: sleep_score, sleep_duration_hours, resting_hr, hrv_ms,
    stress_avg, stress_max, body_battery_high/low, steps, active_calories,
    training_readiness_score (0–100) and training_readiness_level
    (READY/MODERATE/LOW/CAUTION), vo2_max_running, vo2_max_cycling,
    intensity_minutes_moderate, intensity_minutes_vigorous, respiration_avg,
    weight_kg, body_fat_pct, muscle_mass_kg.
    """
    return tools.get_daily_summary(start_date, end_date)


@mcp.tool
def get_training_load_trend(weeks: int = 8) -> list[dict[str, Any]]:
    """Return daily {date, ctl, atl, tsb} for the given number of weeks back."""
    return tools.get_training_load_trend(weeks=weeks)


@mcp.tool
def get_weekly_load(weeks: int = 8) -> list[dict[str, Any]]:
    """Return per-week training load by sport plus a total."""
    return tools.get_weekly_load(weeks=weeks)


@mcp.tool
def get_weight_trend(weeks: int = 12) -> list[dict[str, Any]]:
    """Return weight measurements with 7-day and 28-day moving averages."""
    return tools.get_weight_trend(weeks=weeks)


@mcp.tool
def get_current_plan() -> dict[str, Any] | None:
    """Return the most recent weekly plan with is_current=true."""
    return tools.get_current_plan()


@mcp.tool
def search_sessions(filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Flexible session search.

    Supported filter keys: date_from, date_to, sport_type, min_rpe, max_rpe,
    min_duration_min, has_pain. All optional. Unknown keys raise ValueError.
    """
    return tools.search_sessions(filters)


@mcp.tool
def readiness_today() -> dict[str, Any]:
    """Composite snapshot for today's training decision.

    Returns last-night sleep/HRV/RHR/respiration, Garmin's own training
    readiness score and level, current VO2 max (running + cycling), week-to-
    date intensity minutes, weight delta vs 7d avg, current TSB, and recent
    RPE trend. Prefer ``garmin_readiness.score`` when present; fall back to
    HRV/RHR/sleep heuristics otherwise.
    """
    return tools.readiness_today()


@mcp.tool
def log_session(
    rpe: int | None = None,
    pain_score: int | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    activity_id: str | None = None,
) -> dict[str, Any]:
    """Write a manual log. If activity_id is provided the log is linked to it."""
    return tools.log_session(
        activity_id=activity_id,
        rpe=rpe,
        pain_score=pain_score,
        notes=notes,
        tags=tags,
    )


@mcp.tool
def save_weekly_plan(week_of: str, plan: list[dict[str, Any]], notes: str = "") -> dict[str, Any]:
    """Save a new weekly plan, marking any previous current plan for that week superseded.

    Pushes the plan to Notion inline when its content differs from the prior
    version, so the new pages appear in the Notion plan database before the
    tool returns. If the saved plan is byte-identical to the previous version,
    the mirror is skipped to avoid clobbering any in-progress logging in the
    Notion exercise tables. Use ``sync_plan_to_notion`` to force a refresh.

    REQUIRED before calling this tool when the plan includes any cycling
    session: call ``get_weather_forecast`` for ``week_of`` first and use the
    result to choose indoor vs outdoor for each ride. Prefer indoor (trainer)
    on days with precipitation_probability_max ≥ 70%, precipitation_sum_mm > 3,
    thunderstorm conditions, or wind_speed_max_kmh > 35. Reflect the choice in
    the session ``title`` (e.g. "Z2 Endurance 80 min — indoor trainer") and
    ``description``. If the forecast horizon does not cover the whole week,
    note this in the affected sessions' description.

    Arguments:
      week_of: ISO date (YYYY-MM-DD) of the week's Monday.
      plan: list of session dicts. Each session MUST use these keys:
        - date: ISO date YYYY-MM-DD (required)
        - session_type: one of "cycling" | "running" | "swimming" | "lifting" |
          "walking" | "mobility" | "rest" | "other" (required)
        - title: short label shown as the calendar/Notion event name (required)
        - duration_min: integer minutes (required; downstream defaults to 60 if missing)
        - description: longer description of the session (required)
        - intensity: "Easy" | "Moderate" | "Hard" (optional)
        - time: "HH:MM" 24h, Europe/Oslo (optional; defaults to 08:00)
        - notes: free-text notes (optional)
      notes: optional notes about the whole week.

    Example session:
      {"date": "2026-05-15", "session_type": "cycling",
       "title": "Z2 Endurance 80 min", "duration_min": 80,
       "description": "Steady Z2 ride, 125-138 bpm, 85-95 RPM.",
       "intensity": "Easy", "time": "12:30"}
    """
    return tools.save_weekly_plan(week_of=week_of, plan=plan, notes=notes)


@mcp.tool
def sync_plan_to_notion() -> dict[str, Any]:
    """Force a refresh of the current weekly plan into the Notion plan database.

    Use only when ``save_weekly_plan`` reports ``notion_mirrored: false`` or when
    the Notion pages have drifted from Postgres. Normal saves already mirror
    automatically when the plan content changes; calling this on an unchanged
    plan will re-archive Notion pages and wipe any in-progress logging in their
    exercise tables.

    Returns ``{"notion_mirrored": bool, "notion_skipped_reason": str | None}``.
    """
    return tools.sync_plan_to_notion()


@mcp.tool
def update_athlete_context(updates: dict[str, Any]) -> dict[str, Any]:
    """Upsert the single-row athlete profile.

    Supported fields: ftp_watts, max_hr, body_weight_kg, current_phase, notes.
    """
    return tools.update_athlete_context(updates)


@mcp.tool
def get_weather_forecast(days: int = 7) -> dict[str, Any]:
    """Return a daily weather forecast for the athlete's training location.

    Use this when planning the week to choose between indoor (trainer) and
    outdoor cycling. Heavy rain, thunderstorms, or strong wind are good
    reasons to prefer indoor; mild conditions favor outdoor.

    Args:
      days: forecast horizon in days, 1-16. Default 7.

    Returns:
      {"latitude": float, "longitude": float, "timezone": str,
       "daily": [{"date": "YYYY-MM-DD",
                  "temperature_max_c", "temperature_min_c",
                  "precipitation_sum_mm", "precipitation_probability_max_pct",
                  "wind_speed_max_kmh", "weather_code", "condition"}, ...]}
    """
    return tools.get_weather_forecast(days=days)


__all__ = ["mcp"]
