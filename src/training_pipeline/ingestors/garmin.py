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
    return int(value)


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


class GarminIngestor(IngestorBase):
    def __init__(
        self,
        *,
        client: Any = None,
        client_factory: Callable[[str], Any] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        self._now = now

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

        from garminconnect import Garmin  # type: ignore[import-not-found]

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
                if self._activity_exists(session, source_id):
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
        merged_raw = dict(strava.raw or {})
        merged_raw["garmin_supplement"] = garmin_mapped["raw"]
        strava.raw = merged_raw
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
            if all(v is None for v in (user_summary, sleep, hrv, readiness)):
                cursor += timedelta(days=1)
                continue
            us = user_summary if isinstance(user_summary, dict) else {}
            summary = {
                "date": cursor,
                "source": "garmin",
                "sleep_score": _extract_sleep_score(sleep),
                "sleep_duration_seconds": _extract_sleep_duration(sleep),
                "resting_hr": _coerce_int(us.get("restingHeartRate")),
                "hrv_ms": _extract_hrv_ms(hrv),
                "stress_avg": _coerce_int(us.get("averageStressLevel")),
                "body_battery_high": _coerce_int(us.get("bodyBatteryHighestValue")),
                "body_battery_low": _coerce_int(us.get("bodyBatteryLowestValue")),
                "steps": _coerce_int(us.get("totalSteps")),
                "raw": {
                    "user_summary": user_summary,
                    "sleep": sleep,
                    "hrv": hrv,
                    "training_readiness": readiness,
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
