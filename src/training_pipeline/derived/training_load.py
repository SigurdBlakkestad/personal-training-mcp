import math
from typing import TypedDict

# HR-based TRIMP defaults. ATHLETE_HR_REST / ATHLETE_HR_MAX move into athlete_context
# in step 9; until then we use widely-applicable constants for an adult male athlete.
DEFAULT_REST_HR = 50
DEFAULT_MAX_HR = 190
TRIMP_B_MALE = 1.92


class ActivityInput(TypedDict, total=False):
    duration_seconds: int | None
    normalized_power: int | None
    avg_hr: int | None


def compute_tss_from_power(duration_seconds: int, normalized_power: int, ftp: int) -> float:
    intensity_factor = normalized_power / ftp
    return (duration_seconds * normalized_power * intensity_factor) / (ftp * 3600) * 100


def compute_trimp(
    duration_seconds: int,
    avg_hr: int,
    *,
    rest_hr: int = DEFAULT_REST_HR,
    max_hr: int = DEFAULT_MAX_HR,
    b: float = TRIMP_B_MALE,
) -> float:
    if max_hr <= rest_hr:
        return 0.0
    hr_ratio = (avg_hr - rest_hr) / (max_hr - rest_hr)
    if hr_ratio <= 0:
        return 0.0
    duration_min = duration_seconds / 60.0
    return duration_min * hr_ratio * 0.64 * math.exp(b * hr_ratio)


def compute_training_load(
    activity: ActivityInput,
    *,
    ftp: int,
    rest_hr: int = DEFAULT_REST_HR,
    max_hr: int = DEFAULT_MAX_HR,
    b: float = TRIMP_B_MALE,
) -> float | None:
    duration_seconds = activity.get("duration_seconds")
    if duration_seconds is None or duration_seconds <= 0:
        return None
    normalized_power = activity.get("normalized_power")
    if normalized_power is not None and normalized_power > 0 and ftp > 0:
        return compute_tss_from_power(duration_seconds, normalized_power, ftp)
    avg_hr = activity.get("avg_hr")
    if avg_hr is not None and avg_hr > 0:
        return compute_trimp(duration_seconds, avg_hr, rest_hr=rest_hr, max_hr=max_hr, b=b)
    return None
