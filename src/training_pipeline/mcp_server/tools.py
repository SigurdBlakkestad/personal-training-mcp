"""Coaching tool implementations exposed via the MCP server.

Each tool is split into a public wrapper that opens a database session and an
underscore-prefixed implementation that accepts a session. The wrappers are
registered with FastMCP in server.py; the implementations are exercised
directly in unit tests with mocked sessions.
"""

from datetime import UTC, datetime, time, timedelta
from datetime import date as date_type
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from training_pipeline.notion_sync.client import NotionClient
from training_pipeline.notion_sync.plan_mirror import mirror_plan
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.db import get_session
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import (
    Activity,
    AthleteContext,
    BodyMeasurement,
    DailySummary,
    DerivedMetric,
    ManualLog,
    WeeklyPlan,
)

logger = get_logger(__name__)

ACTIVITY_RESULT_CAP = 100
ATHLETE_CONTEXT_FIELDS = (
    "ftp_watts",
    "max_hr",
    "body_weight_kg",
    "current_phase",
    "notes",
)


def _start_of_window(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _serialize_activity(activity: Activity, log: ManualLog | None) -> dict[str, Any]:
    duration_min = (
        round(activity.duration_seconds / 60.0, 2)
        if activity.duration_seconds is not None
        else None
    )
    moving_time_min = (
        round(activity.moving_time_seconds / 60.0, 2)
        if activity.moving_time_seconds is not None
        else None
    )
    distance_km = (
        round(activity.distance_meters / 1000.0, 3)
        if activity.distance_meters is not None
        else None
    )
    avg_speed_kmh = (
        round(activity.average_speed_ms * 3.6, 2) if activity.average_speed_ms is not None else None
    )
    return {
        "id": str(activity.id),
        "source": activity.source,
        "source_id": activity.source_id,
        "start_time": activity.start_time.isoformat(),
        "sport_type": activity.sport_type,
        "name": activity.name,
        "description": activity.description,
        "duration_min": duration_min,
        "moving_time_min": moving_time_min,
        "distance_km": distance_km,
        "avg_speed_kmh": avg_speed_kmh,
        "is_trainer": activity.is_trainer,
        "workout_type": activity.workout_type,
        "avg_hr": activity.avg_hr,
        "max_hr": activity.max_hr,
        "min_hr": activity.min_hr,
        "avg_power": activity.avg_power,
        "max_power": activity.max_power,
        "normalized_power": activity.normalized_power,
        "kilojoules": activity.kilojoules,
        "calories": activity.calories,
        "suffer_score": activity.suffer_score,
        "training_load": activity.training_load,
        "aerobic_training_effect": activity.aerobic_training_effect,
        "anaerobic_training_effect": activity.anaerobic_training_effect,
        "training_effect_label": activity.training_effect_label,
        "vo2_max": activity.vo2_max,
        "moderate_intensity_minutes": activity.moderate_intensity_minutes,
        "vigorous_intensity_minutes": activity.vigorous_intensity_minutes,
        "avg_stride_length_cm": activity.avg_stride_length_cm,
        "avg_ground_contact_time_ms": activity.avg_ground_contact_time_ms,
        "rpe": log.rpe if log is not None else None,
        "pain": log.pain_score if log is not None else None,
        "notes": log.notes if log is not None else None,
        "tags": list(log.tags) if log is not None and log.tags is not None else None,
    }


def _latest_log_for(session: Session, activity_id: UUID) -> ManualLog | None:
    return session.scalar(
        select(ManualLog)
        .where(ManualLog.activity_id == activity_id)
        .order_by(desc(ManualLog.logged_at))
        .limit(1)
    )


# ---------------------------------------------------------------------------
# READ tools
# ---------------------------------------------------------------------------


def _get_recent_activities(
    session: Session, days: int, sport_type: str | None
) -> list[dict[str, Any]]:
    start = _start_of_window(days)
    stmt = (
        select(Activity)
        .where(Activity.start_time >= start)
        .order_by(desc(Activity.start_time))
        .limit(ACTIVITY_RESULT_CAP)
    )
    if sport_type is not None:
        stmt = stmt.where(Activity.sport_type == sport_type)
    activities = list(session.scalars(stmt))
    rows = [_serialize_activity(a, _latest_log_for(session, a.id)) for a in activities]
    logger.info(
        "mcp.get_recent_activities",
        days=days,
        sport_type=sport_type,
        result_count=len(rows),
    )
    return rows


def get_recent_activities(days: int = 14, sport_type: str | None = None) -> list[dict[str, Any]]:
    with get_session() as session:
        return _get_recent_activities(session, days=days, sport_type=sport_type)


def _get_activity_by_id(session: Session, activity_id: str) -> dict[str, Any] | None:
    try:
        uid = UUID(activity_id)
    except ValueError:
        logger.warning("mcp.get_activity_by_id.invalid_uuid", activity_id=activity_id)
        return None
    activity = session.get(Activity, uid)
    if activity is None:
        logger.info("mcp.get_activity_by_id.not_found", activity_id=activity_id)
        return None
    log = _latest_log_for(session, activity.id)
    payload = _serialize_activity(activity, log)
    payload["raw"] = activity.raw
    logger.info("mcp.get_activity_by_id", activity_id=activity_id, found=True)
    return payload


def get_activity_by_id(activity_id: str) -> dict[str, Any] | None:
    with get_session() as session:
        return _get_activity_by_id(session, activity_id)


def _get_daily_summary(
    session: Session, start_date: date_type, end_date: date_type
) -> list[dict[str, Any]]:
    start_dt = datetime.combine(start_date, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end_date, time.max, tzinfo=UTC)

    summaries = list(
        session.scalars(
            select(DailySummary)
            .where(DailySummary.date >= start_date)
            .where(DailySummary.date <= end_date)
            .order_by(DailySummary.date)
        )
    )
    measurements = list(
        session.scalars(
            select(BodyMeasurement)
            .where(BodyMeasurement.measured_at >= start_dt)
            .where(BodyMeasurement.measured_at <= end_dt)
            .order_by(BodyMeasurement.measured_at)
        )
    )

    by_date: dict[date_type, dict[str, Any]] = {}

    def _empty(day: date_type) -> dict[str, Any]:
        return {
            "date": day.isoformat(),
            "sources": [],
            "sleep_score": None,
            "sleep_duration_hours": None,
            "resting_hr": None,
            "hrv_ms": None,
            "stress_avg": None,
            "stress_max": None,
            "body_battery_high": None,
            "body_battery_low": None,
            "steps": None,
            "active_calories": None,
            "training_readiness_score": None,
            "training_readiness_level": None,
            "vo2_max_running": None,
            "vo2_max_cycling": None,
            "intensity_minutes_moderate": None,
            "intensity_minutes_vigorous": None,
            "respiration_avg": None,
            "weight_kg": None,
            "body_fat_pct": None,
            "muscle_mass_kg": None,
        }

    for summary in summaries:
        row = by_date.setdefault(summary.date, _empty(summary.date))
        row["sources"].append(summary.source)
        if summary.sleep_score is not None:
            row["sleep_score"] = summary.sleep_score
        if summary.sleep_duration_seconds is not None:
            row["sleep_duration_hours"] = round(summary.sleep_duration_seconds / 3600.0, 2)
        if summary.resting_hr is not None:
            row["resting_hr"] = summary.resting_hr
        if summary.hrv_ms is not None:
            row["hrv_ms"] = summary.hrv_ms
        if summary.stress_avg is not None:
            row["stress_avg"] = summary.stress_avg
        if summary.stress_max is not None:
            row["stress_max"] = summary.stress_max
        if summary.body_battery_high is not None:
            row["body_battery_high"] = summary.body_battery_high
        if summary.body_battery_low is not None:
            row["body_battery_low"] = summary.body_battery_low
        if summary.steps is not None:
            row["steps"] = summary.steps
        if summary.active_calories is not None:
            row["active_calories"] = summary.active_calories
        if summary.training_readiness_score is not None:
            row["training_readiness_score"] = summary.training_readiness_score
        if summary.training_readiness_level is not None:
            row["training_readiness_level"] = summary.training_readiness_level
        if summary.vo2_max_running is not None:
            row["vo2_max_running"] = summary.vo2_max_running
        if summary.vo2_max_cycling is not None:
            row["vo2_max_cycling"] = summary.vo2_max_cycling
        if summary.intensity_minutes_moderate is not None:
            row["intensity_minutes_moderate"] = summary.intensity_minutes_moderate
        if summary.intensity_minutes_vigorous is not None:
            row["intensity_minutes_vigorous"] = summary.intensity_minutes_vigorous
        if summary.respiration_avg is not None:
            row["respiration_avg"] = summary.respiration_avg

    for measurement in measurements:
        day = measurement.measured_at.date()
        row = by_date.setdefault(day, _empty(day))
        if measurement.weight_kg is not None:
            row["weight_kg"] = measurement.weight_kg
        if measurement.body_fat_pct is not None:
            row["body_fat_pct"] = measurement.body_fat_pct
        if measurement.muscle_mass_kg is not None:
            row["muscle_mass_kg"] = measurement.muscle_mass_kg

    rows = [by_date[d] for d in sorted(by_date.keys())]
    logger.info(
        "mcp.get_daily_summary",
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        result_count=len(rows),
    )
    return rows


def get_daily_summary(start_date: str, end_date: str) -> list[dict[str, Any]]:
    start = date_type.fromisoformat(start_date)
    end = date_type.fromisoformat(end_date)
    with get_session() as session:
        return _get_daily_summary(session, start, end)


def _metric_series(
    session: Session, metric_name: str, start: date_type, end: date_type
) -> list[tuple[date_type, float]]:
    rows = session.execute(
        select(DerivedMetric.date, DerivedMetric.value)
        .where(DerivedMetric.metric_name == metric_name)
        .where(DerivedMetric.date >= start)
        .where(DerivedMetric.date <= end)
        .order_by(DerivedMetric.date)
    ).all()
    return [(d, float(v)) for d, v in rows]


def _get_training_load_trend(session: Session, weeks: int) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date()
    start = today - timedelta(weeks=weeks)
    ctl = dict(_metric_series(session, "ctl", start, today))
    atl = dict(_metric_series(session, "atl", start, today))
    tsb = dict(_metric_series(session, "tsb", start, today))
    dates = sorted(set(ctl) | set(atl) | set(tsb))
    rows = [
        {
            "date": d.isoformat(),
            "ctl": ctl.get(d),
            "atl": atl.get(d),
            "tsb": tsb.get(d),
        }
        for d in dates
    ]
    logger.info("mcp.get_training_load_trend", weeks=weeks, result_count=len(rows))
    return rows


def get_training_load_trend(weeks: int = 8) -> list[dict[str, Any]]:
    with get_session() as session:
        return _get_training_load_trend(session, weeks)


def _get_weekly_load(session: Session, weeks: int) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date()
    start = today - timedelta(weeks=weeks)
    rows = session.execute(
        select(DerivedMetric.date, DerivedMetric.metric_name, DerivedMetric.value)
        .where(DerivedMetric.metric_name.like("weekly_load_%"))
        .where(DerivedMetric.date >= start)
        .order_by(DerivedMetric.date)
    ).all()

    by_week: dict[date_type, dict[str, Any]] = {}
    for week_of, metric, value in rows:
        bucket = by_week.setdefault(
            week_of,
            {
                "week_of": week_of.isoformat(),
                "cycling_hours": 0.0,
                "running_hours": 0.0,
                "lifting_hours": 0.0,
                "total_load": 0.0,
            },
        )
        suffix = metric.removeprefix("weekly_load_")
        if suffix == "total":
            bucket["total_load"] = float(value)
        elif suffix in {"cycling", "running", "lifting"}:
            bucket[f"{suffix}_hours"] = float(value)
    result = [by_week[w] for w in sorted(by_week.keys())]
    logger.info("mcp.get_weekly_load", weeks=weeks, result_count=len(result))
    return result


def get_weekly_load(weeks: int = 8) -> list[dict[str, Any]]:
    with get_session() as session:
        return _get_weekly_load(session, weeks)


def _get_weight_trend(session: Session, weeks: int) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date()
    start = today - timedelta(weeks=weeks)
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)

    measurements = session.execute(
        select(BodyMeasurement.measured_at, BodyMeasurement.weight_kg)
        .where(BodyMeasurement.weight_kg.is_not(None))
        .where(BodyMeasurement.measured_at >= start_dt)
        .order_by(BodyMeasurement.measured_at)
    ).all()
    avg7 = dict(_metric_series(session, "weight_7d_avg", start, today))
    avg28 = dict(_metric_series(session, "weight_28d_avg", start, today))

    rows: list[dict[str, Any]] = []
    seen_dates: set[date_type] = set()
    for measured_at, weight in measurements:
        day = measured_at.date()
        if day in seen_dates:
            continue
        seen_dates.add(day)
        rows.append(
            {
                "date": day.isoformat(),
                "weight_kg": float(weight) if weight is not None else None,
                "weight_7d_avg": avg7.get(day),
                "weight_28d_avg": avg28.get(day),
            }
        )
    logger.info("mcp.get_weight_trend", weeks=weeks, result_count=len(rows))
    return rows


def get_weight_trend(weeks: int = 12) -> list[dict[str, Any]]:
    with get_session() as session:
        return _get_weight_trend(session, weeks)


def _get_current_plan(session: Session) -> dict[str, Any] | None:
    plan = session.scalar(
        select(WeeklyPlan)
        .where(WeeklyPlan.is_current.is_(True))
        .order_by(desc(WeeklyPlan.week_of), desc(WeeklyPlan.version))
        .limit(1)
    )
    if plan is None:
        logger.info("mcp.get_current_plan.empty")
        return None
    payload = {
        "id": str(plan.id),
        "week_of": plan.week_of.isoformat(),
        "version": plan.version,
        "plan": plan.plan,
        "notes": plan.notes,
        "is_current": plan.is_current,
        "created_at": plan.created_at.isoformat(),
    }
    logger.info(
        "mcp.get_current_plan",
        week_of=plan.week_of.isoformat(),
        sessions=len(plan.plan) if isinstance(plan.plan, list) else 0,
    )
    return payload


def get_current_plan() -> dict[str, Any] | None:
    with get_session() as session:
        return _get_current_plan(session)


def _search_sessions(session: Session, filters: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = {
        "date_from",
        "date_to",
        "sport_type",
        "min_rpe",
        "max_rpe",
        "min_duration_min",
        "has_pain",
    }
    unknown = set(filters) - allowed
    if unknown:
        raise ValueError(f"unsupported filters: {sorted(unknown)}")

    stmt = select(Activity).order_by(desc(Activity.start_time)).limit(ACTIVITY_RESULT_CAP)

    if "date_from" in filters and filters["date_from"] is not None:
        start = datetime.combine(
            date_type.fromisoformat(str(filters["date_from"])), time.min, tzinfo=UTC
        )
        stmt = stmt.where(Activity.start_time >= start)
    if "date_to" in filters and filters["date_to"] is not None:
        end = datetime.combine(
            date_type.fromisoformat(str(filters["date_to"])), time.max, tzinfo=UTC
        )
        stmt = stmt.where(Activity.start_time <= end)
    if "sport_type" in filters and filters["sport_type"] is not None:
        stmt = stmt.where(Activity.sport_type == filters["sport_type"])
    if "min_duration_min" in filters and filters["min_duration_min"] is not None:
        seconds = int(filters["min_duration_min"]) * 60
        stmt = stmt.where(Activity.duration_seconds >= seconds)

    activities = list(session.scalars(stmt))

    min_rpe = filters.get("min_rpe")
    max_rpe = filters.get("max_rpe")
    has_pain = filters.get("has_pain")

    rows: list[dict[str, Any]] = []
    for activity in activities:
        log = _latest_log_for(session, activity.id)
        if min_rpe is not None and (log is None or log.rpe is None or log.rpe < int(min_rpe)):
            continue
        if max_rpe is not None and (log is None or log.rpe is None or log.rpe > int(max_rpe)):
            continue
        if has_pain is True:
            if log is None or log.pain_score is None or log.pain_score <= 0:
                continue
        elif has_pain is False:
            if log is not None and log.pain_score is not None and log.pain_score > 0:
                continue
        rows.append(_serialize_activity(activity, log))
    logger.info("mcp.search_sessions", filters=filters, result_count=len(rows))
    return rows


def search_sessions(filters: dict[str, Any]) -> list[dict[str, Any]]:
    with get_session() as session:
        return _search_sessions(session, filters)


def _readiness_today(session: Session) -> dict[str, Any]:
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    horizon = today - timedelta(days=21)
    horizon_dt = datetime.combine(horizon, time.min, tzinfo=UTC)

    latest_summary = session.scalar(
        select(DailySummary)
        .where(DailySummary.date <= today)
        .order_by(desc(DailySummary.date))
        .limit(1)
    )

    latest_weight_row = session.execute(
        select(BodyMeasurement.measured_at, BodyMeasurement.weight_kg)
        .where(BodyMeasurement.weight_kg.is_not(None))
        .where(BodyMeasurement.measured_at <= datetime.combine(today, time.max, tzinfo=UTC))
        .order_by(desc(BodyMeasurement.measured_at))
        .limit(1)
    ).first()
    latest_weight = float(latest_weight_row[1]) if latest_weight_row is not None else None
    latest_weight_date = (
        latest_weight_row[0].date().isoformat() if latest_weight_row is not None else None
    )

    weight_7d_avg_row = session.execute(
        select(DerivedMetric.value)
        .where(DerivedMetric.metric_name == "weight_7d_avg")
        .order_by(desc(DerivedMetric.date))
        .limit(1)
    ).first()
    weight_7d_avg = float(weight_7d_avg_row[0]) if weight_7d_avg_row is not None else None
    weight_delta_7d = (
        round(latest_weight - weight_7d_avg, 3)
        if latest_weight is not None and weight_7d_avg is not None
        else None
    )

    tsb_row = session.execute(
        select(DerivedMetric.value, DerivedMetric.date)
        .where(DerivedMetric.metric_name == "tsb")
        .order_by(desc(DerivedMetric.date))
        .limit(1)
    ).first()
    current_tsb = float(tsb_row[0]) if tsb_row is not None else None
    current_tsb_date = tsb_row[1].isoformat() if tsb_row is not None else None

    recent_rpe = session.execute(
        select(ManualLog.rpe)
        .where(ManualLog.rpe.is_not(None))
        .where(ManualLog.logged_at >= horizon_dt)
        .order_by(desc(ManualLog.logged_at))
        .limit(7)
    ).all()
    rpe_values = [int(r[0]) for r in recent_rpe if r[0] is not None]
    rpe_avg = round(sum(rpe_values) / len(rpe_values), 2) if rpe_values else None

    latest_garmin_summary = session.scalar(
        select(DailySummary)
        .where(DailySummary.source == "garmin")
        .where(DailySummary.date <= today)
        .order_by(desc(DailySummary.date))
        .limit(1)
    )

    payload = {
        "as_of": today.isoformat(),
        "last_night": {
            "date": latest_summary.date.isoformat() if latest_summary is not None else None,
            "sleep_score": latest_summary.sleep_score if latest_summary is not None else None,
            "sleep_duration_hours": (
                round(latest_summary.sleep_duration_seconds / 3600.0, 2)
                if latest_summary is not None and latest_summary.sleep_duration_seconds is not None
                else None
            ),
            "resting_hr": latest_summary.resting_hr if latest_summary is not None else None,
            "hrv_ms": latest_summary.hrv_ms if latest_summary is not None else None,
            "respiration_avg": (
                latest_summary.respiration_avg if latest_summary is not None else None
            ),
            "body_battery_low": (
                latest_summary.body_battery_low if latest_summary is not None else None
            ),
        },
        "garmin_readiness": {
            "date": (
                latest_garmin_summary.date.isoformat()
                if latest_garmin_summary is not None
                else None
            ),
            "score": (
                latest_garmin_summary.training_readiness_score
                if latest_garmin_summary is not None
                else None
            ),
            "level": (
                latest_garmin_summary.training_readiness_level
                if latest_garmin_summary is not None
                else None
            ),
        },
        "fitness": {
            "vo2_max_running": (
                latest_garmin_summary.vo2_max_running if latest_garmin_summary is not None else None
            ),
            "vo2_max_cycling": (
                latest_garmin_summary.vo2_max_cycling if latest_garmin_summary is not None else None
            ),
            "intensity_minutes_moderate": (
                latest_garmin_summary.intensity_minutes_moderate
                if latest_garmin_summary is not None
                else None
            ),
            "intensity_minutes_vigorous": (
                latest_garmin_summary.intensity_minutes_vigorous
                if latest_garmin_summary is not None
                else None
            ),
        },
        "latest_weight": {
            "date": latest_weight_date,
            "weight_kg": latest_weight,
            "weight_7d_avg": weight_7d_avg,
            "delta_vs_7d": weight_delta_7d,
        },
        "training_load": {
            "tsb": current_tsb,
            "as_of": current_tsb_date,
        },
        "recent_rpe": {
            "window_days": 21,
            "count": len(rpe_values),
            "values": rpe_values,
            "avg": rpe_avg,
        },
    }
    logger.info("mcp.readiness_today", as_of=today.isoformat())
    # yesterday referenced for callers checking freshness; not in payload directly
    _ = yesterday
    return payload


def readiness_today() -> dict[str, Any]:
    with get_session() as session:
        return _readiness_today(session)


# ---------------------------------------------------------------------------
# WRITE tools
# ---------------------------------------------------------------------------


def _log_session(
    session: Session,
    activity_id: str | None,
    rpe: int | None,
    pain_score: int | None,
    notes: str | None,
    tags: list[str] | None,
) -> dict[str, Any]:
    linked_activity: UUID | None = None
    if activity_id is not None:
        try:
            linked_activity = UUID(activity_id)
        except ValueError as exc:
            raise ValueError(f"activity_id is not a valid UUID: {activity_id}") from exc
        exists = session.get(Activity, linked_activity)
        if exists is None:
            raise ValueError(f"activity not found: {activity_id}")

    if rpe is not None and not 1 <= int(rpe) <= 10:
        raise ValueError("rpe must be between 1 and 10")
    if pain_score is not None and not 0 <= int(pain_score) <= 10:
        raise ValueError("pain_score must be between 0 and 10")

    log = ManualLog(
        logged_at=datetime.now(UTC),
        activity_id=linked_activity,
        rpe=int(rpe) if rpe is not None else None,
        pain_score=int(pain_score) if pain_score is not None else None,
        notes=notes,
        tags=list(tags) if tags else None,
    )
    session.add(log)
    session.flush()
    logger.info(
        "mcp.log_session",
        activity_id=activity_id,
        rpe=rpe,
        pain_score=pain_score,
        tags=tags,
        log_id=str(log.id),
    )
    return {
        "id": str(log.id),
        "logged_at": log.logged_at.isoformat(),
        "activity_id": str(log.activity_id) if log.activity_id is not None else None,
        "rpe": log.rpe,
        "pain_score": log.pain_score,
        "notes": log.notes,
        "tags": list(log.tags) if log.tags is not None else None,
    }


def log_session(
    activity_id: str | None = None,
    rpe: int | None = None,
    pain_score: int | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    with get_session() as session:
        return _log_session(session, activity_id, rpe, pain_score, notes, tags)


def _validate_exercises(session_index: int, raw: Any) -> None:
    if raw is None:
        return
    if not isinstance(raw, list):
        raise ValueError(f"session[{session_index}].exercises must be a list")
    for ex_index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"session[{session_index}].exercises[{ex_index}] must be a dict")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"session[{session_index}].exercises[{ex_index}].name must be a non-empty string"
            )
        if "sets" in item and not isinstance(item["sets"], int):
            raise ValueError(f"session[{session_index}].exercises[{ex_index}].sets must be an int")
        if "reps" in item and not isinstance(item["reps"], int | str):
            raise ValueError(
                f"session[{session_index}].exercises[{ex_index}].reps must be int or str"
            )
        if "weight_kg" in item and not isinstance(item["weight_kg"], int | float):
            raise ValueError(
                f"session[{session_index}].exercises[{ex_index}].weight_kg must be a number"
            )
        if "notes" in item and not isinstance(item["notes"], str):
            raise ValueError(
                f"session[{session_index}].exercises[{ex_index}].notes must be a string"
            )


def _mirror_plan_to_notion(session: Session) -> tuple[bool, str | None]:
    """Push the current weekly plan to Notion inline. Returns (mirrored, reason).

    Reason is non-None when the mirror was skipped (config missing) or failed.
    Errors are caught so a Notion outage never rolls back the Postgres write —
    Notion is a mirror, not the source of truth.
    """
    settings = get_settings()
    if not settings.NOTION_TOKEN:
        return False, "notion_token_missing"
    if not settings.NOTION_DB_PLAN_ID:
        return False, "notion_db_plan_id_missing"
    try:
        client = NotionClient(settings.NOTION_TOKEN)
        result = mirror_plan(session, client, settings.NOTION_DB_PLAN_ID)
        logger.info("mcp.notion_mirror.success", **result)
        return True, None
    except Exception as exc:  # noqa: BLE001 -- never roll back the Postgres save
        logger.warning(
            "mcp.notion_mirror.failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return False, f"notion_error:{exc.__class__.__name__}"


def _save_weekly_plan(
    session: Session,
    week_of: date_type,
    plan: list[dict[str, Any]],
    notes: str,
) -> dict[str, Any]:
    if not isinstance(plan, list) or not all(isinstance(item, dict) for item in plan):
        raise ValueError("plan must be a list of dicts")
    for idx, item in enumerate(plan):
        _validate_exercises(idx, item.get("exercises"))

    previous = list(
        session.scalars(
            select(WeeklyPlan)
            .where(WeeklyPlan.week_of == week_of)
            .where(WeeklyPlan.is_current.is_(True))
        )
    )
    prior_plan_content = previous[0].plan if previous else None
    for row in previous:
        row.is_current = False

    max_version = session.scalar(
        select(func.coalesce(func.max(WeeklyPlan.version), 0)).where(WeeklyPlan.week_of == week_of)
    )
    next_version = int(max_version or 0) + 1

    new_plan = WeeklyPlan(
        week_of=week_of,
        version=next_version,
        plan=plan,
        notes=notes or None,
        is_current=True,
    )
    session.add(new_plan)
    session.flush()
    logger.info(
        "mcp.save_weekly_plan",
        week_of=week_of.isoformat(),
        version=next_version,
        sessions=len(plan),
        replaced=len(previous),
    )

    notion_mirrored = False
    notion_skipped_reason: str | None = None
    # Only mirror when the plan content actually changed. Re-mirroring an
    # unchanged plan would archive any in-progress logging the athlete did
    # in the Notion table (done reps / Kg / RPE cells) during the session.
    if prior_plan_content == plan:
        notion_skipped_reason = "unchanged_from_prior_version"
    else:
        notion_mirrored, notion_skipped_reason = _mirror_plan_to_notion(session)

    return {
        "id": str(new_plan.id),
        "week_of": new_plan.week_of.isoformat(),
        "version": new_plan.version,
        "is_current": new_plan.is_current,
        "sessions": len(plan),
        "replaced_versions": [r.version for r in previous],
        "notion_mirrored": notion_mirrored,
        "notion_skipped_reason": notion_skipped_reason,
    }


def save_weekly_plan(week_of: str, plan: list[dict[str, Any]], notes: str = "") -> dict[str, Any]:
    parsed_week = date_type.fromisoformat(week_of)
    with get_session() as session:
        return _save_weekly_plan(session, parsed_week, plan, notes)


def _sync_plan_to_notion(session: Session) -> dict[str, Any]:
    mirrored, reason = _mirror_plan_to_notion(session)
    return {"notion_mirrored": mirrored, "notion_skipped_reason": reason}


def sync_plan_to_notion() -> dict[str, Any]:
    with get_session() as session:
        return _sync_plan_to_notion(session)


def _update_athlete_context(session: Session, updates: dict[str, Any]) -> dict[str, Any]:
    unknown = set(updates) - set(ATHLETE_CONTEXT_FIELDS)
    if unknown:
        raise ValueError(f"unsupported athlete_context fields: {sorted(unknown)}")

    values: dict[str, Any] = {"id": 1, "updated_at": datetime.now(UTC)}
    for field in ATHLETE_CONTEXT_FIELDS:
        if field in updates:
            values[field] = updates[field]

    set_columns = {k: v for k, v in values.items() if k not in {"id"}}
    stmt = pg_insert(AthleteContext).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AthleteContext.id],
        set_=set_columns,
    )
    session.execute(stmt)
    session.flush()

    row = session.get(AthleteContext, 1)
    if row is None:  # pragma: no cover - defensive; upsert just ran
        raise RuntimeError("athlete_context upsert did not persist")

    logger.info("mcp.update_athlete_context", updated_fields=sorted(updates.keys()))
    return {
        "id": row.id,
        "ftp_watts": row.ftp_watts,
        "max_hr": row.max_hr,
        "body_weight_kg": row.body_weight_kg,
        "current_phase": row.current_phase,
        "notes": row.notes,
        "updated_at": row.updated_at.isoformat(),
    }


def update_athlete_context(updates: dict[str, Any]) -> dict[str, Any]:
    with get_session() as session:
        return _update_athlete_context(session, updates)


def get_weather_forecast(days: int = 7) -> dict[str, Any]:
    from training_pipeline.mcp_server.weather import fetch_forecast

    return fetch_forecast(days=days)
