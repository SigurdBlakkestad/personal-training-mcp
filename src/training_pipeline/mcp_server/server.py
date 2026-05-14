"""FastMCP server exposing coaching tools to Claude.

Tool implementations live in `tools.py`. This module registers them with
FastMCP so they are discoverable over the MCP protocol.
"""

from typing import Any

from fastmcp import FastMCP

from training_pipeline.mcp_server import tools

mcp: FastMCP[Any] = FastMCP("personal-training")


@mcp.tool
def get_recent_activities(days: int = 14, sport_type: str | None = None) -> list[dict[str, Any]]:
    """Return recent activities with linked RPE/pain from the latest manual log."""
    return tools.get_recent_activities(days=days, sport_type=sport_type)


@mcp.tool
def get_activity_by_id(activity_id: str) -> dict[str, Any] | None:
    """Return a single activity by UUID with the linked manual log (if any)."""
    return tools.get_activity_by_id(activity_id)


@mcp.tool
def get_daily_summary(start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Return per-day metrics merged from body_measurements and daily_summary.

    Dates are ISO-8601 (YYYY-MM-DD) and inclusive on both ends.
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
    """Composite of last-night sleep/HRV/RHR, weight delta, current TSB, RPE trend."""
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

    `week_of` is ISO date of the week's Monday. `plan` is a list of session dicts.
    """
    return tools.save_weekly_plan(week_of=week_of, plan=plan, notes=notes)


@mcp.tool
def update_athlete_context(updates: dict[str, Any]) -> dict[str, Any]:
    """Upsert the single-row athlete profile.

    Supported fields: ftp_watts, max_hr, body_weight_kg, current_phase, notes.
    """
    return tools.update_athlete_context(updates)


__all__ = ["mcp"]
