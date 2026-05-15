from __future__ import annotations

import base64
import io
import os
import tarfile
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from structlog.stdlib import BoundLogger

from training_pipeline.ingestors.base import IngestionResult, IngestorBase
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import Activity, IngestionRun

logger = get_logger(__name__)

GARMIN_DEFAULT_LOOKBACK_DAYS = 30
GARMIN_PAGE_SIZE = 20
STRAVA_DEDUPE_WINDOW_SECONDS = 60

# Columns Garmin alone provides — when a Garmin activity merges into an
# existing Strava row, copy these onto the Strava row so the rich data is not
# trapped inside ``raw.garmin_supplement``. avg_hr/max_hr/calories etc. are
# excluded because Strava already populates them.
GARMIN_ONLY_ACTIVITY_FIELDS: tuple[str, ...] = (
    "aerobic_training_effect",
    "anaerobic_training_effect",
    "training_effect_label",
    "vo2_max",
    "moderate_intensity_minutes",
    "vigorous_intensity_minutes",
    "min_hr",
    "avg_stride_length_cm",
    "avg_ground_contact_time_ms",
)

SPORT_TYPE_MAP: dict[str, str] = {
    "cycling": "cycling",
    "road_biking": "cycling",
    "indoor_cycling": "cycling",
    "mountain_biking": "cycling",
    "gravel_cycling": "cycling",
    "virtual_ride": "cycling",
    "cyclocross": "cycling",
    "track_cycling": "cycling",
    "downhill_biking": "cycling",
    "recumbent_cycling": "cycling",
    "ebike_mountain_biking": "cycling",
    "ebike_road_biking": "cycling",
    "running": "running",
    "trail_running": "running",
    "treadmill_running": "running",
    "track_running": "running",
    "indoor_running": "running",
    "obstacle_run": "running",
    "ultra_run": "running",
    "lap_swimming": "swimming",
    "open_water_swimming": "swimming",
    "swimming": "swimming",
    "strength_training": "lifting",
    "walking": "walking",
    "indoor_walking": "walking",
    "casual_walking": "walking",
    "speed_walking": "walking",
    "hiking": "walking",
}


def _normalize_sport(type_key: str | None) -> str:
    if not type_key:
        return "other"
    return SPORT_TYPE_MAP.get(type_key, "other")


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _parse_garmin_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _extract_cadence(activity: dict[str, Any]) -> int | None:
    for key in (
        "averageBikingCadenceInRevPerMinute",
        "averageRunningCadenceInStepsPerMinute",
        "averageCadenceInStepsPerMinute",
    ):
        value = activity.get(key)
        if value is not None:
            return int(value)
    return None


def decode_tokens_to_dir(b64: str) -> str:
    raw = base64.b64decode(b64)
    tmp_dir = tempfile.mkdtemp(prefix="garmin-tokens-")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(tmp_dir, filter="data")
    nested = os.path.join(tmp_dir, ".garminconnect")
    if os.path.isdir(nested):
        return nested
    return tmp_dir


def _extract_sleep_score(sleep: Any) -> int | None:
    if not isinstance(sleep, dict):
        return None
    daily = sleep.get("dailySleepDTO") or {}
    scores = daily.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    return _coerce_int(overall.get("value"))


def _extract_sleep_duration(sleep: Any) -> int | None:
    if not isinstance(sleep, dict):
        return None
    daily = sleep.get("dailySleepDTO") or {}
    return _coerce_int(daily.get("sleepTimeSeconds"))


def _extract_hrv_ms(hrv: Any) -> float | None:
    if not isinstance(hrv, dict):
        return None
    summary = hrv.get("hrvSummary") or {}
    value = summary.get("lastNightAvg")
    if value is None:
        return None
    return float(value)


def _extract_readiness(readiness: Any) -> tuple[int | None, str | None]:
    """Garmin returns readiness as either a dict or a list of dicts.

    The morning snapshot (``inputContext == "AFTER_WAKEUP_RESET"``) is preferred
    when present; otherwise we fall back to the first entry.
    """
    entry: dict[str, Any] | None = None
    if isinstance(readiness, dict):
        entry = readiness
    elif isinstance(readiness, list) and readiness:
        morning = next(
            (
                e
                for e in readiness
                if isinstance(e, dict) and e.get("inputContext") == "AFTER_WAKEUP_RESET"
            ),
            None,
        )
        first = readiness[0] if isinstance(readiness[0], dict) else None
        entry = morning or first
    if entry is None:
        return None, None
    return _coerce_int(entry.get("score")), _coerce_str(entry.get("level"))


def _extract_vo2_max(max_metrics: Any) -> tuple[float | None, float | None]:
    """Returns (running, cycling) VO2 max from get_max_metrics().

    The endpoint returns a list with one dict that has ``generic`` (running) and
    ``cycling`` sub-objects, each holding ``vo2MaxPreciseValue``.
    """
    entry: dict[str, Any] | None = None
    if isinstance(max_metrics, list) and max_metrics:
        first = max_metrics[0]
        if isinstance(first, dict):
            entry = first
    elif isinstance(max_metrics, dict):
        entry = max_metrics
    if entry is None:
        return None, None
    running = _vo2_from_block(entry.get("generic"))
    cycling = _vo2_from_block(entry.get("cycling"))
    return running, cycling


def _vo2_from_block(block: Any) -> float | None:
    if not isinstance(block, dict):
        return None
    for key in ("vo2MaxPreciseValue", "vo2MaxValue"):
        value = _coerce_float(block.get(key))
        if value is not None:
            return value
    return None


def _extract_intensity_minutes(intensity: Any) -> tuple[int | None, int | None]:
    if not isinstance(intensity, dict):
        return None, None
    return (
        _coerce_int(intensity.get("moderateMinutes")),
        _coerce_int(intensity.get("vigorousMinutes")),
    )


def _extract_respiration_avg(respiration: Any) -> float | None:
    if not isinstance(respiration, dict):
        return None
    for key in ("avgWakingRespirationValue", "avgSleepRespirationValue"):
        value = _coerce_float(respiration.get(key))
        if value is not None:
            return value
    return None


class GarminIngestor(IngestorBase):
    def __init__(
        self,
        *,
        client: Any = None,
        client_factory: Callable[[str], Any] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        backfill: bool = False,
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        self._now = now
        self._backfill = backfill

    @property
    def name(self) -> str:
        return "Garmin"

    @property
    def source_key(self) -> str:
        return "garmin"

    def _sync(self, session: Session, since: datetime | None) -> IngestionResult:
        log = logger.bind(source="garmin")

        client = self._client if self._client is not None else self._initialize_client()
        effective_since = since if since is not None else self._compute_since(session)
        log.info("garmin.fetch.start", since=effective_since.isoformat())

        result = IngestionResult()
        self._sync_activities(client, session, effective_since, result, log)
        self._sync_daily_summaries(client, session, effective_since, result, log)

        log.info(
            "garmin.fetch.done",
            records_processed=result.records_processed,
            records_inserted=result.records_inserted,
            records_updated=result.records_updated,
        )
        return result

    def _initialize_client(self) -> Any:
        settings = get_settings()
        if not settings.GARMINTOKENS_B64:
            raise RuntimeError(
                "GARMINTOKENS_B64 is not set. Run scripts/garmin_auth.py locally first."
            )
        tokens_path = decode_tokens_to_dir(settings.GARMINTOKENS_B64)
        if self._client_factory is not None:
            return self._client_factory(tokens_path)

        from garminconnect import Garmin  # type: ignore[import-untyped]

        client = Garmin()
        client.login(tokenstore=tokens_path)
        return client

    def _compute_since(self, session: Session) -> datetime:
        latest = session.scalar(
            select(IngestionRun.finished_at)
            .where(IngestionRun.source == "garmin", IngestionRun.status == "success")
            .order_by(desc(IngestionRun.finished_at))
            .limit(1)
        )
        if latest is None:
            return self._now() - timedelta(days=GARMIN_DEFAULT_LOOKBACK_DAYS)
        return latest

    def _sync_activities(
        self,
        client: Any,
        session: Session,
        since: datetime,
        result: IngestionResult,
        log: BoundLogger,
    ) -> None:
        start = 0
        while True:
            batch = client.get_activities(start=start, limit=GARMIN_PAGE_SIZE) or []
            if not batch:
                break
            stop = False
            for activity in batch:
                start_time = _parse_garmin_time(activity.get("startTimeGMT"))
                if start_time is None:
                    log.warning("garmin.activity.skip_no_start", id=activity.get("activityId"))
                    continue
                if start_time < since:
                    stop = True
                    break
                source_id = str(activity["activityId"])
                if not self._backfill and self._activity_exists(session, source_id):
                    stop = True
                    break
                mapped = self._map_activity(activity, start_time)
                if self._merge_into_strava_if_exists(session, mapped, log):
                    result.records_processed += 1
                    result.records_updated += 1
                    continue
                outcome = self.upsert_activity(session, mapped)
                result.records_processed += 1
                if outcome == "inserted":
                    result.records_inserted += 1
                else:
                    result.records_updated += 1
            if stop or len(batch) < GARMIN_PAGE_SIZE:
                break
            start += GARMIN_PAGE_SIZE

    def _activity_exists(self, session: Session, source_id: str) -> bool:
        found = session.scalar(
            select(Activity.id).where(
                Activity.source == "garmin",
                Activity.source_id == source_id,
            )
        )
        return found is not None

    def _merge_into_strava_if_exists(
        self,
        session: Session,
        garmin_mapped: dict[str, Any],
        log: BoundLogger,
    ) -> bool:
        start_time = garmin_mapped["start_time"]
        window = timedelta(seconds=STRAVA_DEDUPE_WINDOW_SECONDS)
        strava = session.scalar(
            select(Activity).where(
                Activity.source == "strava",
                Activity.start_time >= start_time - window,
                Activity.start_time <= start_time + window,
            )
        )
        if strava is None:
            return False
        strava.garmin_supplement = garmin_mapped["raw"]
        for field in GARMIN_ONLY_ACTIVITY_FIELDS:
            value = garmin_mapped.get(field)
            if value is not None and getattr(strava, field, None) is None:
                setattr(strava, field, value)
        log.info(
            "garmin.activity.merged_into_strava",
            strava_source_id=strava.source_id,
            garmin_source_id=garmin_mapped["source_id"],
        )
        return True

    def _map_activity(self, activity: dict[str, Any], start_time: datetime) -> dict[str, Any]:
        duration = activity.get("duration")
        end_time = start_time + timedelta(seconds=duration) if duration is not None else None
        activity_type = activity.get("activityType")
        type_key = activity_type.get("typeKey") if isinstance(activity_type, dict) else None
        return {
            "source": "garmin",
            "source_id": str(activity["activityId"]),
            "start_time": start_time,
            "end_time": end_time,
            "sport_type": _normalize_sport(type_key),
            "name": activity.get("activityName"),
            "duration_seconds": _coerce_int(duration),
            "distance_meters": activity.get("distance"),
            "elevation_gain_meters": activity.get("elevationGain"),
            "avg_hr": _coerce_int(activity.get("averageHR")),
            "max_hr": _coerce_int(activity.get("maxHR")),
            "avg_power": _coerce_int(activity.get("avgPower")),
            "normalized_power": _coerce_int(activity.get("normPower")),
            "calories": _coerce_int(activity.get("calories")),
            "avg_cadence": _extract_cadence(activity),
            "aerobic_training_effect": _coerce_float(activity.get("aerobicTrainingEffect")),
            "anaerobic_training_effect": _coerce_float(activity.get("anaerobicTrainingEffect")),
            "training_effect_label": _coerce_str(activity.get("trainingEffectLabel")),
            "vo2_max": _coerce_float(activity.get("vO2MaxValue")),
            "moderate_intensity_minutes": _coerce_int(activity.get("moderateIntensityMinutes")),
            "vigorous_intensity_minutes": _coerce_int(activity.get("vigorousIntensityMinutes")),
            "min_hr": _coerce_int(activity.get("minHR")),
            "max_power": _coerce_int(activity.get("maxPower") or activity.get("maxAvgPower")),
            "avg_stride_length_cm": _coerce_float(activity.get("avgStrideLength")),
            "avg_ground_contact_time_ms": _coerce_int(activity.get("avgGroundContactTime")),
            "raw": activity,
        }

    def _sync_daily_summaries(
        self,
        client: Any,
        session: Session,
        since: datetime,
        result: IngestionResult,
        log: BoundLogger,
    ) -> None:
        today = self._now().date()
        if self._backfill:
            cursor = since.date()
        else:
            earliest = today - timedelta(days=GARMIN_DEFAULT_LOOKBACK_DAYS)
            cursor = max(since.date(), earliest)
        while cursor <= today:
            iso = cursor.isoformat()
            user_summary = self._safe_call(client.get_user_summary, iso, log, "user_summary")
            sleep = self._safe_call(client.get_sleep_data, iso, log, "sleep_data")
            hrv = self._safe_call(client.get_hrv_data, iso, log, "hrv_data")
            readiness = self._safe_call(
                client.get_training_readiness, iso, log, "training_readiness"
            )
            max_metrics = self._safe_call(client.get_max_metrics, iso, log, "max_metrics")
            intensity = self._safe_call(
                client.get_intensity_minutes_data, iso, log, "intensity_minutes"
            )
            respiration = self._safe_call(client.get_respiration_data, iso, log, "respiration")
            if all(
                v is None
                for v in (user_summary, sleep, hrv, readiness, max_metrics, intensity, respiration)
            ):
                cursor += timedelta(days=1)
                continue
            us = user_summary if isinstance(user_summary, dict) else {}
            readiness_score, readiness_level = _extract_readiness(readiness)
            vo2_running, vo2_cycling = _extract_vo2_max(max_metrics)
            im_moderate, im_vigorous = _extract_intensity_minutes(intensity)
            summary = {
                "date": cursor,
                "source": "garmin",
                "sleep_score": _extract_sleep_score(sleep),
                "sleep_duration_seconds": _extract_sleep_duration(sleep),
                "resting_hr": _coerce_int(us.get("restingHeartRate")),
                "hrv_ms": _extract_hrv_ms(hrv),
                "stress_avg": _coerce_int(us.get("averageStressLevel")),
                "stress_max": _coerce_int(us.get("maxStressLevel")),
                "body_battery_high": _coerce_int(us.get("bodyBatteryHighestValue")),
                "body_battery_low": _coerce_int(us.get("bodyBatteryLowestValue")),
                "steps": _coerce_int(us.get("totalSteps")),
                "active_calories": _coerce_int(us.get("activeKilocalories")),
                "training_readiness_score": readiness_score,
                "training_readiness_level": readiness_level,
                "vo2_max_running": vo2_running,
                "vo2_max_cycling": vo2_cycling,
                "intensity_minutes_moderate": im_moderate,
                "intensity_minutes_vigorous": im_vigorous,
                "respiration_avg": _extract_respiration_avg(respiration),
                "raw": {
                    "user_summary": user_summary,
                    "sleep": sleep,
                    "hrv": hrv,
                    "training_readiness": readiness,
                    "max_metrics": max_metrics,
                    "intensity_minutes": intensity,
                    "respiration": respiration,
                },
            }
            outcome = self.upsert_daily_summary(session, summary)
            result.records_processed += 1
            if outcome == "inserted":
                result.records_inserted += 1
            else:
                result.records_updated += 1
            cursor += timedelta(days=1)

    def _safe_call(
        self,
        func: Callable[[str], Any],
        iso_date: str,
        log: BoundLogger,
        endpoint: str,
    ) -> Any:
        try:
            return func(iso_date)
        except Exception as exc:  # noqa: BLE001 -- Garmin endpoints throw on missing data
            log.warning(
                "garmin.daily.endpoint_failed",
                endpoint=endpoint,
                date=iso_date,
                error=str(exc),
            )
            return None
