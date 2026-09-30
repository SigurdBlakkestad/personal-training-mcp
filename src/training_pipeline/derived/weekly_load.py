from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TypedDict

from training_pipeline.shared.local_time import local_date

TRACKED_SPORTS: tuple[str, ...] = ("cycling", "running", "lifting")
TOTAL_KEY = "total"


@dataclass(frozen=True)
class WeeklyLoad:
    week_of: date
    sport: str
    load: float


class WeeklyLoadInput(TypedDict, total=False):
    start_time: datetime
    sport_type: str | None
    training_load: float | None


def _iso_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def sum_by_week_and_sport(
    entries: Iterable[tuple[datetime, str | None, float]],
) -> dict[tuple[date, str], float]:
    """Sum (start_time, sport_type, value) entries per ISO week and sport.

    Weeks start on Monday (ISO) in the athlete's local timezone. Entries with
    unknown or untracked sport_type still contribute to the 'total' bucket.
    """
    buckets: dict[tuple[date, str], float] = defaultdict(float)
    for start_time, sport, value in entries:
        week = _iso_monday(local_date(start_time))
        if sport in TRACKED_SPORTS:
            buckets[(week, sport)] += value
        buckets[(week, TOTAL_KEY)] += value
    return buckets


def compute_weekly_loads(
    activities: Iterable[WeeklyLoadInput],
) -> list[WeeklyLoad]:
    """Aggregate per-activity training load into per-week per-sport totals."""
    entries: list[tuple[datetime, str | None, float]] = []
    for activity in activities:
        load = activity.get("training_load")
        start_time = activity.get("start_time")
        if load is None or start_time is None:
            continue
        entries.append((start_time, activity.get("sport_type"), load))
    return [
        WeeklyLoad(week_of=week, sport=sport, load=load)
        for (week, sport), load in sorted(sum_by_week_and_sport(entries).items())
    ]
