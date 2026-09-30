"""Coaching tool implementations exposed via the MCP server.

Each tool is split into a public wrapper that opens a database session and an
underscore-prefixed implementation that accepts a session. The wrappers are
registered with FastMCP in server.py; the implementations are exercised
directly in unit tests with mocked sessions.
"""

import math
from datetime import UTC, datetime, time, timedelta
from datetime import date as date_type
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from training_pipeline.derived.weekly_load import (
    TOTAL_KEY,
    TRACKED_SPORTS,
    sum_by_week_and_sport,
)
from training_pipeline.notion_sync.client import NotionClient
from training_pipeline.notion_sync.plan_mirror import (
    INTENSITY_OPTIONS,
    SESSION_TYPE_ALIASES,
    mirror_plan,
)
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.db import get_session
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import (
    Activity,
    ActivityExerciseSet,
    ActivityLap,
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
        "garmin_training_load": activity.garmin_training_load,
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


def _serialize_laps(session: Session, activity_id: UUID) -> list[dict[str, Any]]:
    laps = session.scalars(
        select(ActivityLap)
        .where(ActivityLap.activity_id == activity_id)
        .order_by(ActivityLap.lap_index)
    )
    return [
        {
            "lap_index": lap.lap_index,
            "lap_type": lap.lap_type,
            "duration_s": lap.duration_s,
            "duration_min": (
                round(lap.duration_s / 60.0, 2) if lap.duration_s is not None else None
            ),
            "moving_duration_s": lap.moving_duration_s,
            "distance_km": (
                round(lap.distance_meters / 1000.0, 3) if lap.distance_meters is not None else None
            ),
            "avg_power": lap.avg_power,
            "max_power": lap.max_power,
            "normalized_power": lap.normalized_power,
            "avg_hr": lap.avg_hr,
            "max_hr": lap.max_hr,
            "avg_cadence": lap.avg_cadence,
        }
        for lap in laps
    ]


def _serialize_exercise_sets(session: Session, activity_id: UUID) -> list[dict[str, Any]]:
    sets = session.scalars(
        select(ActivityExerciseSet)
        .where(ActivityExerciseSet.activity_id == activity_id)
        .order_by(ActivityExerciseSet.set_index)
    )
    return [
        {
            "set_index": s.set_index,
            "set_type": s.set_type,
            # Garmin classifies the movement rather than being told it, so this
            # is a guess — read it together with exercise_confidence.
            "exercise_name": s.exercise_name,
            "exercise_confidence": s.exercise_confidence,
            "reps": s.reps,
            "weight_kg": s.weight_kg,
            "duration_s": s.duration_s,
        }
        for s in sets
    ]


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
    # Per-lap and per-set detail when the ingestor captured it. Both stay out
    # of the list-shaped tools: they are only worth the rows once a single
    # session is being looked at.
    laps = _serialize_laps(session, activity.id)
    exercise_sets = _serialize_exercise_sets(session, activity.id)
    if laps:
        payload["laps"] = laps
    if exercise_sets:
        payload["exercise_sets"] = exercise_sets
    payload["raw"] = activity.raw
    logger.info(
        "mcp.get_activity_by_id",
        activity_id=activity_id,
        found=True,
        lap_count=len(laps),
        exercise_set_count=len(exercise_sets),
    )
    return payload


def get_activity_by_id(activity_id: str) -> dict[str, Any] | None:
    with get_session() as session:
        return _get_activity_by_id(session, activity_id)


# Fields copied verbatim from ``daily_summary`` rows into the merged per-day row.
_DAILY_SUMMARY_FIELDS = (
    "sleep_score",
    "resting_hr",
    "hrv_ms",
    "stress_avg",
    "stress_max",
    "body_battery_high",
    "body_battery_low",
    "steps",
    "active_calories",
    "training_readiness_score",
    "training_readiness_level",
    "vo2_max_running",
    "vo2_max_cycling",
    "intensity_minutes_moderate",
    "intensity_minutes_vigorous",
    "respiration_avg",
)


def _empty_daily_row(day: date_type) -> dict[str, Any]:
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


def _source_priority(summary: DailySummary) -> tuple[int, str]:
    """Garmin first, then any other source alphabetically."""
    return (0 if summary.source == "garmin" else 1, summary.source)


def _merge_daily_summaries(
    summaries: list[DailySummary],
) -> dict[date_type, dict[str, Any]]:
    """Merge per-source ``daily_summary`` rows into one row per date.

    The Garmin row is authoritative: other sources (Withings) only fill
    fields Garmin left empty for that date. Deterministic regardless of the
    order the rows arrive in.
    """
    by_date: dict[date_type, dict[str, Any]] = {}
    for summary in sorted(summaries, key=lambda s: (s.date, _source_priority(s))):
        row = by_date.setdefault(summary.date, _empty_daily_row(summary.date))
        row["sources"].append(summary.source)
        if row["sleep_duration_hours"] is None and summary.sleep_duration_seconds is not None:
            row["sleep_duration_hours"] = round(summary.sleep_duration_seconds / 3600.0, 2)
        for field in _DAILY_SUMMARY_FIELDS:
            if row[field] is None:
                row[field] = getattr(summary, field)
    return by_date


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

    by_date = _merge_daily_summaries(summaries)

    for measurement in measurements:
        day = measurement.measured_at.date()
        row = by_date.setdefault(day, _empty_daily_row(day))
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
    """Per ISO week (Monday ``week_of``): load points and hours per sport.

    ``*_load`` / ``total_load`` are summed training-load points (the
    ``weekly_load_*`` derived metrics). ``*_hours`` / ``total_hours`` are
    activity durations in hours, bucketed the same way as the load.
    """
    today = datetime.now(UTC).date()
    start = today - timedelta(weeks=weeks)
    load_rows = session.execute(
        select(DerivedMetric.date, DerivedMetric.metric_name, DerivedMetric.value)
        .where(DerivedMetric.metric_name.like("weekly_load_%"))
        .where(DerivedMetric.date >= start)
        .order_by(DerivedMetric.date)
    ).all()
    # Only weeks whose Monday is on or after `start` are reported (same rule as
    # the load rows), so every activity in those weeks starts after `start`.
    activity_rows = session.execute(
        select(
            Activity.start_time,
            Activity.sport_type,
            func.coalesce(Activity.duration_seconds, 0),
        ).where(Activity.start_time >= datetime.combine(start, time.min, tzinfo=UTC))
    ).all()
    seconds = sum_by_week_and_sport(
        (start_time, sport, float(duration)) for start_time, sport, duration in activity_rows
    )

    by_week: dict[date_type, dict[str, Any]] = {}

    def bucket_for(week_of: date_type) -> dict[str, Any]:
        return by_week.setdefault(
            week_of,
            {"week_of": week_of.isoformat()}
            | {f"{key}_load": 0.0 for key in (*TRACKED_SPORTS, TOTAL_KEY)}
            | {f"{key}_hours": 0.0 for key in (*TRACKED_SPORTS, TOTAL_KEY)},
        )

    for week_of, metric, value in load_rows:
        key = metric.removeprefix("weekly_load_")
        if key in TRACKED_SPORTS or key == TOTAL_KEY:
            bucket_for(week_of)[f"{key}_load"] = float(value)
    for (week_of, key), total_seconds in seconds.items():
        if week_of >= start:
            bucket_for(week_of)[f"{key}_hours"] = round(total_seconds / 3600, 2)
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
    horizon = today - timedelta(days=21)
    horizon_dt = datetime.combine(horizon, time.min, tzinfo=UTC)
    week_start = today - timedelta(days=today.weekday())

    # "Last night" is Garmin's newest date (Garmin is authoritative); other
    # sources only fill gaps on that date. Without any Garmin row, fall back
    # to the newest date from any source.
    last_night_anchor = func.coalesce(
        select(func.max(DailySummary.date))
        .where(DailySummary.source == "garmin")
        .where(DailySummary.date <= today)
        .scalar_subquery(),
        select(func.max(DailySummary.date)).where(DailySummary.date <= today).scalar_subquery(),
    )
    last_night_rows = list(
        session.scalars(select(DailySummary).where(DailySummary.date == last_night_anchor))
    )
    last_night_date = last_night_rows[0].date if last_night_rows else None
    last_night: dict[str, Any] = (
        _merge_daily_summaries(last_night_rows)[last_night_date]
        if last_night_date is not None
        else {}
    )

    latest_weight_row = session.execute(
        select(BodyMeasurement.measured_at, BodyMeasurement.weight_kg)
        .where(BodyMeasurement.weight_kg.is_not(None))
        .where(BodyMeasurement.measured_at <= datetime.combine(today, time.max, tzinfo=UTC))
        .order_by(desc(BodyMeasurement.measured_at))
        .limit(1)
    ).first()
    latest_weight = latest_weight_row[1] if latest_weight_row is not None else None
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
    garmin_days_old = (
        (today - latest_garmin_summary.date).days if latest_garmin_summary is not None else None
    )
    stale = garmin_days_old is None or garmin_days_old > 1

    week_intensity = session.execute(
        select(
            DailySummary.intensity_minutes_moderate,
            DailySummary.intensity_minutes_vigorous,
        )
        .where(DailySummary.source == "garmin")
        .where(DailySummary.date >= week_start)
        .where(DailySummary.date <= today)
    ).all()
    moderate_values = [int(r[0]) for r in week_intensity if r[0] is not None]
    vigorous_values = [int(r[1]) for r in week_intensity if r[1] is not None]

    payload = {
        "as_of": today.isoformat(),
        "stale": stale,
        "last_night": {
            "data_date": last_night_date.isoformat() if last_night_date is not None else None,
            "days_old": (today - last_night_date).days if last_night_date is not None else None,
            "sources": last_night.get("sources", []),
            "sleep_score": last_night.get("sleep_score"),
            "sleep_duration_hours": last_night.get("sleep_duration_hours"),
            "resting_hr": last_night.get("resting_hr"),
            "hrv_ms": last_night.get("hrv_ms"),
            "respiration_avg": last_night.get("respiration_avg"),
            "body_battery_low": last_night.get("body_battery_low"),
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
            "intensity_minutes_week_to_date": {
                "week_start": week_start.isoformat(),
                "moderate": sum(moderate_values) if moderate_values else None,
                "vigorous": sum(vigorous_values) if vigorous_values else None,
            },
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
    logger.info(
        "mcp.readiness_today",
        as_of=today.isoformat(),
        stale=stale,
        garmin_days_old=garmin_days_old,
    )
    return payload


def readiness_today() -> dict[str, Any]:
    with get_session() as session:
        return _readiness_today(session)


# ---------------------------------------------------------------------------
# WRITE tools
# ---------------------------------------------------------------------------


# Length caps on free text that flows into Notion and the calendar feed.
MAX_TITLE_CHARS = 200
MAX_TAG_CHARS = 200
MAX_NOTES_CHARS = 2000
MAX_DESCRIPTION_CHARS = 4000

# Accepted plan session types: the documented enum plus the synonyms the
# Notion mirror already normalizes. Anything else would silently become
# "Other" downstream.
PLAN_SESSION_TYPES = frozenset(SESSION_TYPE_ALIASES) | {"other"}


def _reject(tool: str, problems: list[str]) -> NoReturn:
    """Reject a write with every problem listed so the caller can fix them in one retry."""
    logger.warning("mcp.write_rejected", tool=tool, problems=problems)
    raise ValueError(f"{tool} rejected, nothing saved: " + "; ".join(problems))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _parse_iso_date(raw: str) -> date_type | None:
    """Parse strict ``YYYY-MM-DD``; Python 3.11+ fromisoformat also takes forms Notion rejects."""
    try:
        parsed = date_type.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == raw else None


def _text_problem(label: str, value: Any, max_chars: int) -> str | None:
    """Return a problem for a non-string or over-long optional text field."""
    if value is None:
        return None
    if not isinstance(value, str):
        return f"{label} must be a string"
    if len(value) > max_chars:
        return f"{label} must be at most {max_chars} characters (got {len(value)})"
    return None


def _log_session(
    session: Session,
    activity_id: str | None,
    rpe: int | None,
    pain_score: int | None,
    notes: str | None,
    tags: list[str] | None,
) -> dict[str, Any]:
    problems: list[str] = []
    has_notes = isinstance(notes, str) and bool(notes.strip())
    if activity_id is None and rpe is None and pain_score is None and not has_notes and not tags:
        problems.append("provide at least one of activity_id, rpe, pain_score, notes or tags")

    linked_activity: UUID | None = None
    if activity_id is not None:
        try:
            linked_activity = UUID(activity_id)
        except ValueError:
            problems.append(f"activity_id is not a valid UUID: {activity_id}")
        else:
            if session.get(Activity, linked_activity) is None:
                problems.append(f"activity not found: {activity_id}")

    if rpe is not None and not (_is_int(rpe) and 1 <= rpe <= 10):
        problems.append("rpe must be an integer between 1 and 10")
    if pain_score is not None and not (_is_int(pain_score) and 0 <= pain_score <= 10):
        problems.append("pain_score must be an integer between 0 and 10")
    notes_problem = _text_problem("notes", notes, MAX_NOTES_CHARS)
    if notes_problem:
        problems.append(notes_problem)
    if tags is not None:
        if not isinstance(tags, list):
            problems.append("tags must be a list of strings")
        else:
            for idx, tag in enumerate(tags):
                if not isinstance(tag, str) or not tag.strip():
                    problems.append(f"tags[{idx}] must be a non-empty string")
                elif len(tag) > MAX_TAG_CHARS:
                    problems.append(f"tags[{idx}] must be at most {MAX_TAG_CHARS} characters")
    if problems:
        _reject("log_session", problems)

    log = ManualLog(
        logged_at=datetime.now(UTC),
        activity_id=linked_activity,
        rpe=rpe,
        pain_score=pain_score,
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


def _exercise_problems(session_index: int, raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [f"session[{session_index}].exercises must be a list"]
    problems: list[str] = []
    for ex_index, item in enumerate(raw):
        prefix = f"session[{session_index}].exercises[{ex_index}]"
        if not isinstance(item, dict):
            problems.append(f"{prefix} must be a dict")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{prefix}.name must be a non-empty string")
        if "sets" in item and not _is_int(item["sets"]):
            problems.append(f"{prefix}.sets must be an int")
        if "reps" in item and not (_is_int(item["reps"]) or isinstance(item["reps"], str)):
            problems.append(f"{prefix}.reps must be int or str")
        if "weight_kg" in item and not _is_number(item["weight_kg"]):
            problems.append(f"{prefix}.weight_kg must be a number")
        if "notes" in item:
            if not isinstance(item["notes"], str):
                problems.append(f"{prefix}.notes must be a string")
            elif len(item["notes"]) > MAX_NOTES_CHARS:
                problems.append(f"{prefix}.notes must be at most {MAX_NOTES_CHARS} characters")
    return problems


def _is_hh_mm(value: str) -> bool:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        return False
    return True


def _plan_session_problems(index: int, item: Any, week_of: date_type | None) -> list[str]:
    """Problems with one plan session. ``week_of`` is None when it didn't parse."""
    prefix = f"session[{index}]"
    if not isinstance(item, dict):
        return [f"{prefix} must be a dict"]
    problems: list[str] = []

    raw_date = item.get("date")
    if not isinstance(raw_date, str):
        problems.append(f"{prefix}.date is required as an ISO date (YYYY-MM-DD)")
    else:
        session_date = _parse_iso_date(raw_date)
        if session_date is None:
            problems.append(f"{prefix}.date is not an ISO date (YYYY-MM-DD): {raw_date!r}")
        elif week_of is not None:
            week_end = week_of + timedelta(days=6)
            if not week_of <= session_date <= week_end:
                problems.append(
                    f"{prefix}.date {raw_date} is outside the week "
                    f"{week_of.isoformat()}..{week_end.isoformat()}"
                )

    session_type = item.get("session_type")
    if not isinstance(session_type, str) or session_type.strip().lower() not in PLAN_SESSION_TYPES:
        problems.append(
            f"{prefix}.session_type must be one of {sorted(PLAN_SESSION_TYPES)}, "
            f"got {session_type!r}"
        )

    if "duration_min" in item and not (_is_int(item["duration_min"]) and item["duration_min"] > 0):
        problems.append(f"{prefix}.duration_min must be a positive integer")

    start_time = item.get("time")
    if start_time is not None and not (isinstance(start_time, str) and _is_hh_mm(start_time)):
        problems.append(f'{prefix}.time must be "HH:MM" (24h), got {start_time!r}')

    intensity = item.get("intensity")
    if intensity is not None and not (
        isinstance(intensity, str) and intensity.strip().title() in INTENSITY_OPTIONS
    ):
        problems.append(
            f"{prefix}.intensity must be one of {sorted(INTENSITY_OPTIONS)}, got {intensity!r}"
        )

    for key, cap in (
        ("title", MAX_TITLE_CHARS),
        ("description", MAX_DESCRIPTION_CHARS),
        ("notes", MAX_NOTES_CHARS),
    ):
        text_problem = _text_problem(f"{prefix}.{key}", item.get(key), cap)
        if text_problem:
            problems.append(text_problem)

    problems.extend(_exercise_problems(index, item.get("exercises")))
    return problems


def _validate_weekly_plan(week_of: date_type | str, plan: Any, notes: Any) -> date_type:
    """Check the whole plan up front; raise one error listing every problem."""
    problems: list[str] = []
    parsed_week: date_type | None = None
    if isinstance(week_of, date_type):
        parsed_week = week_of
    else:
        parsed_week = _parse_iso_date(week_of)
        if parsed_week is None:
            problems.append(f"week_of is not an ISO date (YYYY-MM-DD): {week_of!r}")
    is_monday = parsed_week is not None and parsed_week.weekday() == 0
    if parsed_week is not None and not is_monday:
        problems.append(f"week_of must be a Monday, got {parsed_week.isoformat()}")

    if not isinstance(plan, list):
        problems.append("plan must be a list of dicts")
    else:
        # The date-range check needs a valid week; skip it rather than pile on.
        range_week = parsed_week if is_monday else None
        for idx, item in enumerate(plan):
            problems.extend(_plan_session_problems(idx, item, range_week))

    notes_problem = _text_problem("notes", notes, MAX_NOTES_CHARS)
    if notes_problem:
        problems.append(notes_problem)

    if problems or parsed_week is None:
        _reject("save_weekly_plan", problems)
    return parsed_week


def _mirror_plan_to_notion(
    session: Session, *, week_of: date_type | None = None
) -> tuple[bool, str | None]:
    """Push current weekly plan(s) to Notion inline. Returns (mirrored, reason).

    When ``week_of`` is set, only that week is mirrored (used during a save so
    we don't churn other weeks' Notion pages). When omitted, all is_current
    plans are mirrored (used by the explicit ``sync_plan_to_notion`` tool).

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
        result = mirror_plan(session, client, settings.NOTION_DB_PLAN_ID, week_of=week_of)
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
    week_of: date_type | str,
    plan: list[dict[str, Any]],
    notes: str,
) -> dict[str, Any]:
    week_of = _validate_weekly_plan(week_of, plan, notes)

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
        notion_mirrored, notion_skipped_reason = _mirror_plan_to_notion(session, week_of=week_of)

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
    with get_session() as session:
        return _save_weekly_plan(session, week_of, plan, notes)


def _sync_plan_to_notion(session: Session) -> dict[str, Any]:
    mirrored, reason = _mirror_plan_to_notion(session)
    return {"notion_mirrored": mirrored, "notion_skipped_reason": reason}


def sync_plan_to_notion() -> dict[str, Any]:
    with get_session() as session:
        return _sync_plan_to_notion(session)


def _athlete_context_problems(updates: dict[str, Any]) -> list[str]:
    """Type-check known fields; ``None`` is allowed and clears the field."""
    problems: list[str] = []
    unknown = set(updates) - set(ATHLETE_CONTEXT_FIELDS)
    if unknown:
        problems.append(f"unsupported athlete_context fields: {sorted(unknown)}")
    for key in ("ftp_watts", "max_hr"):
        value = updates.get(key)
        if value is not None and not (_is_int(value) and value > 0):
            problems.append(f"{key} must be a positive integer")
    weight = updates.get("body_weight_kg")
    if weight is not None and not (_is_number(weight) and weight > 0):
        problems.append("body_weight_kg must be a positive number")
    for key, cap in (("current_phase", MAX_TITLE_CHARS), ("notes", MAX_NOTES_CHARS)):
        text_problem = _text_problem(key, updates.get(key), cap)
        if text_problem:
            problems.append(text_problem)
    return problems


def _update_athlete_context(session: Session, updates: dict[str, Any]) -> dict[str, Any]:
    problems = _athlete_context_problems(updates)
    if problems:
        _reject("update_athlete_context", problems)

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
