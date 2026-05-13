from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from training_pipeline.derived.ctl_atl_tsb import compute_ctl_atl_tsb
from training_pipeline.derived.training_load import compute_training_load
from training_pipeline.derived.weekly_load import WeeklyLoadInput, compute_weekly_loads
from training_pipeline.derived.weight_trend import compute_weight_trend
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import Activity, BodyMeasurement, DerivedMetric

logger = get_logger(__name__)

DEFAULT_RECOMPUTE_WINDOW_DAYS = 365
EWMA_WARMUP_DAYS = 90


@dataclass
class RecomputeCounts:
    training_load_updated: int = 0
    ctl_atl_tsb_rows: int = 0
    weekly_load_rows: int = 0
    weight_trend_rows: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "training_load_updated": self.training_load_updated,
            "ctl_atl_tsb_rows": self.ctl_atl_tsb_rows,
            "weekly_load_rows": self.weekly_load_rows,
            "weight_trend_rows": self.weight_trend_rows,
        }


def recompute_all(session: Session, since: date | None = None) -> RecomputeCounts:
    """Recompute all derived metrics and upsert into derived_metrics.

    `since` bounds the rows written to derived_metrics, but training load is
    backfilled and EWMAs (CTL/ATL/TSB) read further history to warm up.
    """
    settings = get_settings()
    today = datetime.now(UTC).date()
    effective_since = (
        since if since is not None else today - timedelta(days=DEFAULT_RECOMPUTE_WINDOW_DAYS)
    )
    log = logger.bind(since=effective_since.isoformat())
    log.info("derived.recompute.start")

    counts = RecomputeCounts()

    warmup_start = datetime.combine(
        effective_since - timedelta(days=EWMA_WARMUP_DAYS), time.min, tzinfo=UTC
    )
    activities = list(
        session.scalars(
            select(Activity)
            .where(Activity.start_time >= warmup_start)
            .order_by(Activity.start_time)
        )
    )

    counts.training_load_updated = _backfill_training_load(activities, ftp=settings.ATHLETE_FTP)
    session.flush()

    counts.ctl_atl_tsb_rows = _upsert_ctl_atl_tsb(session, activities, since=effective_since)
    counts.weekly_load_rows = _upsert_weekly_load(session, activities, since=effective_since)
    counts.weight_trend_rows = _upsert_weight_trend(session, since=effective_since)

    log.info("derived.recompute.done", **counts.to_dict())
    return counts


def _backfill_training_load(activities: Iterable[Activity], *, ftp: int) -> int:
    updated = 0
    for activity in activities:
        if activity.training_load is not None:
            continue
        load = compute_training_load(
            {
                "duration_seconds": activity.duration_seconds,
                "normalized_power": activity.normalized_power,
                "avg_hr": activity.avg_hr,
            },
            ftp=ftp,
        )
        if load is None:
            continue
        activity.training_load = load
        updated += 1
    return updated


def _upsert_ctl_atl_tsb(session: Session, activities: Iterable[Activity], *, since: date) -> int:
    daily_loads: dict[date, float] = defaultdict(float)
    for activity in activities:
        if activity.training_load is None:
            continue
        day = activity.start_time.date()
        daily_loads[day] += activity.training_load

    series = compute_ctl_atl_tsb(list(daily_loads.items()))
    rows: list[dict[str, Any]] = []
    for point in series:
        if point.date < since:
            continue
        rows.append({"date": point.date, "metric_name": "ctl", "value": point.ctl})
        rows.append({"date": point.date, "metric_name": "atl", "value": point.atl})
        rows.append({"date": point.date, "metric_name": "tsb", "value": point.tsb})
    _upsert_metrics(session, rows)
    return len(rows)


def _upsert_weekly_load(session: Session, activities: Iterable[Activity], *, since: date) -> int:
    inputs: list[WeeklyLoadInput] = [
        WeeklyLoadInput(
            start_time=a.start_time,
            sport_type=a.sport_type,
            training_load=a.training_load,
        )
        for a in activities
        if a.start_time.date() >= since
    ]
    weekly = compute_weekly_loads(inputs)
    rows: list[dict[str, Any]] = [
        {
            "date": item.week_of,
            "metric_name": f"weekly_load_{item.sport}",
            "value": item.load,
        }
        for item in weekly
    ]
    _upsert_metrics(session, rows)
    return len(rows)


def _upsert_weight_trend(session: Session, *, since: date) -> int:
    history_start = datetime.combine(since - timedelta(days=27), time.min, tzinfo=UTC)
    rows_raw = session.execute(
        select(BodyMeasurement.measured_at, BodyMeasurement.weight_kg)
        .where(BodyMeasurement.weight_kg.is_not(None))
        .where(BodyMeasurement.measured_at >= history_start)
        .order_by(BodyMeasurement.measured_at)
    ).all()
    measurements: list[tuple[date, float]] = [
        (measured_at.date(), float(weight))
        for measured_at, weight in rows_raw
        if weight is not None
    ]
    series = compute_weight_trend(measurements)
    rows: list[dict[str, Any]] = []
    for point in series:
        if point.date < since:
            continue
        if point.weight_7d_avg is not None:
            rows.append(
                {
                    "date": point.date,
                    "metric_name": "weight_7d_avg",
                    "value": point.weight_7d_avg,
                }
            )
        if point.weight_28d_avg is not None:
            rows.append(
                {
                    "date": point.date,
                    "metric_name": "weight_28d_avg",
                    "value": point.weight_28d_avg,
                }
            )
    _upsert_metrics(session, rows)
    return len(rows)


def _upsert_metrics(session: Session, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    stmt = pg_insert(DerivedMetric).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_derived_metrics_date_metric",
        set_={"value": stmt.excluded.value, "computed_at": func.now()},
    )
    session.execute(stmt)
