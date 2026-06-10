import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from structlog.stdlib import BoundLogger

from training_pipeline.ingestors.base import IngestionResult, IngestorBase
from training_pipeline.ingestors.garmin import GARMIN_PRIORITY_FIELDS
from training_pipeline.ingestors.http import HttpClient
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import Activity, IngestionRun

logger = get_logger(__name__)

STRAVA_API_BASE = "https://www.strava.com"
STRAVA_DEFAULT_LOOKBACK_DAYS = 365
STRAVA_PAGE_SIZE = 200
STRAVA_RATE_WINDOW_SECONDS = 900
STRAVA_RATE_LIMIT_THRESHOLD = 0.9
# Strava's /athlete/activities `after` filter is keyed on activity start_date,
# not upload time. An activity uploaded after a sync but with a start_date
# before the next cursor will slip through. Re-scan a 48h window each run; the
# (source, source_id) upsert makes the overlap a no-op for already-seen rows.
STRAVA_OVERLAP_MARGIN_HOURS = 48
# Same window used by the Garmin ingestor when folding into Strava. If the two
# clocks drift more than this, the dedup misses and you'd get one row per side.
GARMIN_DEDUPE_WINDOW_SECONDS = 60

SPORT_TYPE_MAP: dict[str, str] = {
    "Ride": "cycling",
    "MountainBikeRide": "cycling",
    "GravelRide": "cycling",
    "VirtualRide": "cycling",
    "EBikeRide": "cycling",
    "EMountainBikeRide": "cycling",
    "Velomobile": "cycling",
    "Handcycle": "cycling",
    "Run": "running",
    "TrailRun": "running",
    "VirtualRun": "running",
    "Swim": "swimming",
    "WeightTraining": "lifting",
    "Walk": "walking",
    "Hike": "walking",
}


def _normalize_sport(strava_sport_type: str | None) -> str:
    if not strava_sport_type:
        return "other"
    return SPORT_TYPE_MAP.get(strava_sport_type, "other")


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


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


class StravaIngestor(IngestorBase):
    def __init__(
        self,
        *,
        http_client: HttpClient | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        before: datetime | None = None,
    ) -> None:
        self._http = (
            http_client if http_client is not None else HttpClient(base_url=STRAVA_API_BASE)
        )
        self._sleeper = sleeper
        self._now = now
        self._before = before

    @property
    def name(self) -> str:
        return "Strava"

    @property
    def source_key(self) -> str:
        return "strava"

    def _sync(self, session: Session, since: datetime | None) -> IngestionResult:
        log = logger.bind(source="strava")
        settings = get_settings()

        initial_refresh_token = self._lookup_refresh_token(session, settings.STRAVA_REFRESH_TOKEN)
        access_token, new_refresh_token = self._refresh_access_token(
            client_id=settings.STRAVA_CLIENT_ID,
            client_secret=settings.STRAVA_CLIENT_SECRET,
            refresh_token=initial_refresh_token,
        )
        if new_refresh_token != initial_refresh_token:
            log.warning(
                "strava.refresh_token.rotated",
                message=(
                    "Strava issued a new refresh_token. "
                    "Update GitHub Secret STRAVA_REFRESH_TOKEN with the value stored in "
                    "ingestion_runs.cursor."
                ),
            )

        effective_since = since if since is not None else self._compute_since(session)
        after_epoch = int(effective_since.timestamp())
        log.info("strava.fetch.start", after=effective_since.isoformat())

        result = IngestionResult(cursor=json.dumps({"refresh_token": new_refresh_token}))

        page = 1
        auth_headers = {"Authorization": f"Bearer {access_token}"}
        while True:
            params: dict[str, Any] = {
                "after": after_epoch,
                "page": page,
                "per_page": STRAVA_PAGE_SIZE,
            }
            if self._before is not None:
                params["before"] = int(self._before.timestamp())
            response = self._http.get(
                "/api/v3/athlete/activities",
                params=params,
                headers=auth_headers,
            )
            activities = response.json()
            if not activities:
                break
            for activity in activities:
                mapped = self._map_activity(activity)
                if self._consume_garmin_row_if_exists(session, mapped, log):
                    result.records_processed += 1
                    result.records_updated += 1
                    continue
                outcome = self.upsert_activity(session, mapped)
                result.records_processed += 1
                if outcome == "inserted":
                    result.records_inserted += 1
                else:
                    result.records_updated += 1
            self._maybe_pause_for_rate_limit(response.headers, log)
            if len(activities) < STRAVA_PAGE_SIZE:
                break
            page += 1

        log.info(
            "strava.fetch.done",
            pages=page,
            records_processed=result.records_processed,
            records_inserted=result.records_inserted,
            records_updated=result.records_updated,
        )
        return result

    def _lookup_refresh_token(self, session: Session, fallback: str) -> str:
        latest = session.scalar(
            select(IngestionRun)
            .where(IngestionRun.source == "strava", IngestionRun.status == "success")
            .order_by(desc(IngestionRun.finished_at))
            .limit(1)
        )
        if latest is None or not latest.cursor:
            return fallback
        try:
            data = json.loads(latest.cursor)
        except (json.JSONDecodeError, TypeError):
            return fallback
        token = data.get("refresh_token") if isinstance(data, dict) else None
        if isinstance(token, str) and token:
            return token
        return fallback

    def _compute_since(self, session: Session) -> datetime:
        latest = session.scalar(
            select(IngestionRun.finished_at)
            .where(IngestionRun.source == "strava", IngestionRun.status == "success")
            .order_by(desc(IngestionRun.finished_at))
            .limit(1)
        )
        if latest is None:
            return datetime.now(UTC) - timedelta(days=STRAVA_DEFAULT_LOOKBACK_DAYS)
        return latest - timedelta(hours=STRAVA_OVERLAP_MARGIN_HOURS)

    def _refresh_access_token(
        self, *, client_id: str, client_secret: str, refresh_token: str
    ) -> tuple[str, str]:
        response = self._http.post(
            "/oauth/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        payload = response.json()
        access_token: str = payload["access_token"]
        new_refresh: str = payload.get("refresh_token", refresh_token)
        return access_token, new_refresh

    def _consume_garmin_row_if_exists(
        self,
        session: Session,
        mapped: dict[str, Any],
        log: Any,
    ) -> bool:
        """Repurpose an existing Garmin row at the same start_time, if any.

        Symmetric to ``GarminIngestor._merge_into_strava_if_exists`` — when the
        Garmin sync runs first (backfill or just earlier in the morning), the
        activity is sitting in Postgres as ``source='garmin'``. When the Strava
        sync arrives, we don't want to insert a second row; we want the Strava
        record to *become* the canonical row while preserving Garmin's
        exclusives (training effect, VO2, running dynamics) and stashing
        Garmin's raw payload as ``raw.garmin_supplement``.

        Mutating in place (rather than insert-and-delete) keeps any manual_logs
        foreign keys intact.

        Returns True if a Garmin row was consumed; the caller should skip the
        regular upsert. Returns False if no match was found or if there's
        already a Strava row for this source_id (in which case the regular
        upsert path handles it).
        """
        start_time = mapped["start_time"]
        window = timedelta(seconds=GARMIN_DEDUPE_WINDOW_SECONDS)
        garmin = session.scalar(
            select(Activity).where(
                Activity.source == "garmin",
                Activity.start_time >= start_time - window,
                Activity.start_time <= start_time + window,
            )
        )
        if garmin is None:
            return False
        conflict = session.scalar(
            select(Activity.id).where(
                Activity.source == "strava",
                Activity.source_id == mapped["source_id"],
            )
        )
        if conflict is not None:
            return False
        old_garmin_raw = dict(garmin.raw or {})
        # Garmin's device measurements (HR, power, auto-paused duration, ...)
        # are more accurate than Strava's re-processed versions. Preserve them
        # across the consume so the row keeps Garmin's intensity numbers even
        # though its source/source_id flip to Strava.
        preserved = {
            field: getattr(garmin, field)
            for field in GARMIN_PRIORITY_FIELDS
            if getattr(garmin, field, None) is not None
        }
        for key, value in mapped.items():
            setattr(garmin, key, value)
        for field, value in preserved.items():
            setattr(garmin, field, value)
        garmin.garmin_supplement = old_garmin_raw
        garmin.updated_at = datetime.now(UTC)
        log.info(
            "strava.activity.consumed_garmin",
            strava_source_id=mapped["source_id"],
            garmin_row_id=str(garmin.id),
        )
        return True

    def _map_activity(self, activity: dict[str, Any]) -> dict[str, Any]:
        start_time = datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))
        elapsed = activity.get("elapsed_time")
        end_time = start_time + timedelta(seconds=elapsed) if elapsed is not None else None
        return {
            "source": "strava",
            "source_id": str(activity["id"]),
            "start_time": start_time,
            "end_time": end_time,
            "sport_type": _normalize_sport(activity.get("sport_type")),
            "name": activity.get("name"),
            "duration_seconds": elapsed,
            "moving_time_seconds": _coerce_int(activity.get("moving_time")),
            "distance_meters": activity.get("distance"),
            "elevation_gain_meters": activity.get("total_elevation_gain"),
            "avg_hr": _coerce_int(activity.get("average_heartrate")),
            "max_hr": _coerce_int(activity.get("max_heartrate")),
            "avg_power": _coerce_int(activity.get("average_watts")),
            "max_power": _coerce_int(activity.get("max_watts")),
            "normalized_power": _coerce_int(activity.get("weighted_average_watts")),
            "calories": _coerce_int(activity.get("calories")),
            "avg_cadence": _coerce_int(activity.get("average_cadence")),
            "suffer_score": _coerce_int(activity.get("suffer_score")),
            "kilojoules": _coerce_float(activity.get("kilojoules")),
            "average_speed_ms": _coerce_float(activity.get("average_speed")),
            "is_trainer": _coerce_bool(activity.get("trainer")),
            "workout_type": _coerce_int(activity.get("workout_type")),
            "description": activity.get("description") or None,
            "raw": activity,
        }

    def _maybe_pause_for_rate_limit(self, headers: Mapping[str, str], log: BoundLogger) -> None:
        usage = headers.get("X-RateLimit-Usage")
        limit = headers.get("X-RateLimit-Limit")
        if not usage or not limit:
            return
        try:
            short_usage = int(usage.split(",")[0])
            short_limit = int(limit.split(",")[0])
        except (ValueError, IndexError):
            return
        if short_limit <= 0:
            return
        if short_usage / short_limit < STRAVA_RATE_LIMIT_THRESHOLD:
            return
        now_epoch = int(self._now())
        remainder = STRAVA_RATE_WINDOW_SECONDS - (now_epoch % STRAVA_RATE_WINDOW_SECONDS)
        pause = remainder + 1
        log.warning(
            "strava.rate_limit.pause",
            short_usage=short_usage,
            short_limit=short_limit,
            pause_seconds=pause,
        )
        self._sleeper(pause)
