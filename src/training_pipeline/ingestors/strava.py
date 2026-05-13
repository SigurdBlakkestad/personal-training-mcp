import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from structlog.stdlib import BoundLogger

from training_pipeline.ingestors.base import IngestionResult, IngestorBase
from training_pipeline.ingestors.http import HttpClient
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import IngestionRun

logger = get_logger(__name__)

STRAVA_API_BASE = "https://www.strava.com"
STRAVA_DEFAULT_LOOKBACK_DAYS = 365
STRAVA_PAGE_SIZE = 200
STRAVA_RATE_WINDOW_SECONDS = 900
STRAVA_RATE_LIMIT_THRESHOLD = 0.9

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
    return int(value)


class StravaIngestor(IngestorBase):
    def __init__(
        self,
        *,
        http_client: HttpClient | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._http = (
            http_client if http_client is not None else HttpClient(base_url=STRAVA_API_BASE)
        )
        self._sleeper = sleeper
        self._now = now

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
            response = self._http.get(
                "/api/v3/athlete/activities",
                params={"after": after_epoch, "page": page, "per_page": STRAVA_PAGE_SIZE},
                headers=auth_headers,
            )
            activities = response.json()
            if not activities:
                break
            for activity in activities:
                mapped = self._map_activity(activity)
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
        return latest

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
            "distance_meters": activity.get("distance"),
            "elevation_gain_meters": activity.get("total_elevation_gain"),
            "avg_hr": _coerce_int(activity.get("average_heartrate")),
            "max_hr": _coerce_int(activity.get("max_heartrate")),
            "avg_power": _coerce_int(activity.get("average_watts")),
            "normalized_power": _coerce_int(activity.get("weighted_average_watts")),
            "calories": _coerce_int(activity.get("calories")),
            "avg_cadence": _coerce_int(activity.get("average_cadence")),
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
