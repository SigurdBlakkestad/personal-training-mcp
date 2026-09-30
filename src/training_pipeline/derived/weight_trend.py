from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from training_pipeline.shared.local_time import local_date

SHORT_WINDOW_DAYS = 7
LONG_WINDOW_DAYS = 28


@dataclass(frozen=True)
class WeightTrendPoint:
    date: date
    weight_7d_avg: float | None
    weight_28d_avg: float | None


def daily_weights(measurements: Iterable[tuple[datetime, float]]) -> dict[date, float]:
    """One weight per local calendar day: the day's first reading.

    The first weigh-in is the conventional morning, fasted weight; later ones
    drift with food and fluids. Both the derived averages and the weight tool
    use this so the daily value and its trend come from the same reading.
    """
    first: dict[date, tuple[datetime, float]] = {}
    for measured_at, weight in measurements:
        day = local_date(measured_at)
        current = first.get(day)
        if current is None or measured_at < current[0]:
            first[day] = (measured_at, weight)
    return {day: weight for day, (_, weight) in first.items()}


def compute_weight_trend(
    measurements: Iterable[tuple[datetime, float]],
    *,
    short_window: int = SHORT_WINDOW_DAYS,
    long_window: int = LONG_WINDOW_DAYS,
) -> list[WeightTrendPoint]:
    """Compute trailing 7d and 28d moving averages of weight.

    Each local day contributes its first reading (see ``daily_weights``). Days
    with no measurement contribute nothing to the window (we don't forward-fill:
    an old reading should not count as a fresh sample).
    """
    by_day = daily_weights(measurements)

    if not by_day:
        return []

    start = min(by_day)
    end = max(by_day)
    out: list[WeightTrendPoint] = []
    current = start
    while current <= end:
        short_start = current - timedelta(days=short_window - 1)
        long_start = current - timedelta(days=long_window - 1)
        short_vals = [w for d, w in by_day.items() if short_start <= d <= current]
        long_vals = [w for d, w in by_day.items() if long_start <= d <= current]
        short_avg = sum(short_vals) / len(short_vals) if short_vals else None
        long_avg = sum(long_vals) / len(long_vals) if long_vals else None
        out.append(WeightTrendPoint(date=current, weight_7d_avg=short_avg, weight_28d_avg=long_avg))
        current = current + timedelta(days=1)
    return out
