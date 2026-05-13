from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TypedDict

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


def compute_weekly_loads(
    activities: Iterable[WeeklyLoadInput],
) -> list[WeeklyLoad]:
    """Aggregate per-activity training load into per-week per-sport totals.

    Weeks start on Monday (ISO). Activities with unknown or untracked sport_type
    still contribute to the 'total' bucket.
    """
    buckets: dict[tuple[date, str], float] = defaultdict(float)
    for activity in activities:
        load = activity.get("training_load")
        start_time = activity.get("start_time")
        if load is None or start_time is None:
            continue
        week = _iso_monday(start_time.date())
        sport = activity.get("sport_type")
        if sport in TRACKED_SPORTS:
            buckets[(week, sport)] += load
        buckets[(week, TOTAL_KEY)] += load
    return [
        WeeklyLoad(week_of=week, sport=sport, load=load)
        for (week, sport), load in sorted(buckets.items())
    ]
