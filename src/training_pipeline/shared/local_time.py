"""Calendar days and weeks in the athlete's local timezone.

Timestamps are stored in UTC, but "which day did this happen on" and "what is
today" are answered in ``ATHLETE_TZ``: a session at 00:30 Oslo time belongs to
that day (and, on a Monday, to that ISO week), not to the previous UTC day.
"""

from datetime import UTC, date, datetime
from functools import cache
from zoneinfo import ZoneInfo

from training_pipeline.shared.config import get_settings


@cache
def athlete_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().ATHLETE_TZ)


def local_date(dt: datetime) -> date:
    """The athlete's calendar day for an aware timestamp."""
    return dt.astimezone(athlete_tz()).date()


def local_today() -> date:
    return local_date(datetime.now(UTC))
