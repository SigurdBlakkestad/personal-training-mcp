from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

CTL_TIME_CONSTANT_DAYS = 42
ATL_TIME_CONSTANT_DAYS = 7


@dataclass(frozen=True)
class DailyLoadPoint:
    date: date
    ctl: float
    atl: float
    tsb: float


def compute_ctl_atl_tsb(
    daily_loads: Iterable[tuple[date, float]],
    *,
    ctl_tc: int = CTL_TIME_CONSTANT_DAYS,
    atl_tc: int = ATL_TIME_CONSTANT_DAYS,
) -> list[DailyLoadPoint]:
    """Compute daily CTL/ATL/TSB from a per-day training load series.

    Missing days between the first and last load are treated as zero load — this is
    the standard convention so that rest days correctly decay CTL/ATL.
    """
    by_date: dict[date, float] = {}
    for d, load in daily_loads:
        by_date[d] = by_date.get(d, 0.0) + load

    if not by_date:
        return []

    start = min(by_date)
    end = max(by_date)
    ctl = 0.0
    atl = 0.0
    out: list[DailyLoadPoint] = []
    current = start
    while current <= end:
        load = by_date.get(current, 0.0)
        ctl = ctl + (load - ctl) / ctl_tc
        atl = atl + (load - atl) / atl_tc
        out.append(DailyLoadPoint(date=current, ctl=ctl, atl=atl, tsb=ctl - atl))
        current = current + timedelta(days=1)
    return out
