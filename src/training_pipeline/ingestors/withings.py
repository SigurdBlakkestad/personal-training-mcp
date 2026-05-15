import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from training_pipeline.ingestors.base import IngestionResult, IngestorBase
from training_pipeline.ingestors.http import HttpClient
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import IngestionRun

logger = get_logger(__name__)

WITHINGS_API_BASE = "https://wbsapi.withings.net"
WITHINGS_DEFAULT_LOOKBACK_DAYS = 30

# Withings measure type codes → body_measurements column name.
# Only codes that map to schema columns are written; values for unmapped codes
# remain available via the raw JSONB payload.
WITHINGS_MEASURE_TYPE_TO_COLUMN: dict[int, str] = {
    1: "weight_kg",
    6: "body_fat_pct",
    76: "muscle_mass_kg",
    77: "water_pct",
    88: "bone_mass_kg",
}


class WithingsAPIError(Exception):
    pass


class WithingsIngestor(IngestorBase):
    def __init__(self, *, http_client: HttpClient | None = None) -> None:
        self._http = (
            http_client if http_client is not None else HttpClient(base_url=WITHINGS_API_BASE)
        )

    @property
    def name(self) -> str:
        return "Withings"

    @property
    def source_key(self) -> str:
        return "withings"

    def _sync(self, session: Session, since: datetime | None) -> IngestionResult:
        log = logger.bind(source="withings")
        settings = get_settings()

        initial_refresh = self._lookup_refresh_token(session, settings.WITHINGS_REFRESH_TOKEN)
        access_token, new_refresh = self._refresh_access_token(
            client_id=settings.WITHINGS_CLIENT_ID,
            client_secret=settings.WITHINGS_CLIENT_SECRET,
            refresh_token=initial_refresh,
        )
        if new_refresh != initial_refresh:
            log.warning(
                "withings.refresh_token.rotated",
                message=(
                    "Withings issued a new refresh_token. "
                    "Update GitHub Secret WITHINGS_REFRESH_TOKEN with the value stored in "
                    "ingestion_runs.cursor."
                ),
            )

        effective_since = since if since is not None else self._compute_since(session)
        now_utc = datetime.now(UTC)
        log.info("withings.fetch.start", since=effective_since.isoformat())

        result = IngestionResult(cursor=json.dumps({"refresh_token": new_refresh}))
        auth_headers = {"Authorization": f"Bearer {access_token}"}

        self._sync_body_measurements(session, auth_headers, effective_since, now_utc, result, log)
        self._sync_daily(session, auth_headers, effective_since, now_utc, result, log)

        log.info(
            "withings.fetch.done",
            records_processed=result.records_processed,
            records_inserted=result.records_inserted,
            records_updated=result.records_updated,
        )
        return result

    def _lookup_refresh_token(self, session: Session, fallback: str) -> str:
        latest = session.scalar(
            select(IngestionRun)
            .where(IngestionRun.source == "withings", IngestionRun.status == "success")
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
            .where(IngestionRun.source == "withings", IngestionRun.status == "success")
            .order_by(desc(IngestionRun.finished_at))
            .limit(1)
        )
        if latest is None:
            return datetime.now(UTC) - timedelta(days=WITHINGS_DEFAULT_LOOKBACK_DAYS)
        return latest

    def _refresh_access_token(
        self, *, client_id: str, client_secret: str, refresh_token: str
    ) -> tuple[str, str]:
        body = self._post_action(
            "/v2/oauth2",
            data={
                "action": "requesttoken",
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers=None,
        )
        access_token = body["access_token"]
        new_refresh = body.get("refresh_token", refresh_token)
        if not isinstance(access_token, str) or not isinstance(new_refresh, str):
            raise WithingsAPIError("withings oauth response missing string tokens")
        return access_token, new_refresh

    def _post_action(
        self,
        path: str,
        *,
        data: Mapping[str, Any],
        headers: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        response = self._http.post(path, data=data, headers=headers)
        envelope = response.json()
        if not isinstance(envelope, dict):
            raise WithingsAPIError(f"withings response not a dict: path={path}")
        status = envelope.get("status")
        if status != 0:
            raise WithingsAPIError(
                f"withings api error: status={status} error={envelope.get('error')!r} path={path}"
            )
        body = envelope.get("body")
        if not isinstance(body, dict):
            raise WithingsAPIError(f"withings body not a dict: path={path}")
        return body

    def _sync_body_measurements(
        self,
        session: Session,
        headers: Mapping[str, str],
        since: datetime,
        now: datetime,
        result: IngestionResult,
        log: Any,
    ) -> None:
        body = self._post_action(
            "/measure",
            data={
                "action": "getmeas",
                "startdate": int(since.timestamp()),
                "enddate": int(now.timestamp()),
            },
            headers=headers,
        )
        groups = body.get("measuregrps") or []
        for group in groups:
            if not isinstance(group, dict):
                continue
            measurement = self._map_measure_group(group)
            if measurement is None:
                continue
            outcome = self.upsert_body_measurement(session, measurement)
            result.records_processed += 1
            if outcome == "inserted":
                result.records_inserted += 1
            else:
                result.records_updated += 1
        log.info("withings.body.done", groups=len(groups))

    def _map_measure_group(self, group: dict[str, Any]) -> dict[str, Any] | None:
        epoch = group.get("date")
        if epoch is None:
            return None
        measured_at = datetime.fromtimestamp(int(epoch), tz=UTC)
        mapped: dict[str, Any] = {
            "source": "withings",
            "measured_at": measured_at,
            "raw": group,
        }
        for measure in group.get("measures") or []:
            if not isinstance(measure, dict):
                continue
            mtype = measure.get("type")
            value = measure.get("value")
            unit = measure.get("unit", 0)
            if mtype is None or value is None:
                continue
            column = WITHINGS_MEASURE_TYPE_TO_COLUMN.get(int(mtype))
            if column is None:
                continue
            try:
                converted = float(value) * (10 ** int(unit))
            except (TypeError, ValueError):
                continue
            mapped[column] = converted
        return mapped

    def _sync_daily(
        self,
        session: Session,
        headers: Mapping[str, str],
        since: datetime,
        now: datetime,
        result: IngestionResult,
        log: Any,
    ) -> None:
        start_ymd = since.date().isoformat()
        end_ymd = now.date().isoformat()

        activity_body = self._post_action(
            "/v2/measure",
            data={
                "action": "getactivity",
                "startdateymd": start_ymd,
                "enddateymd": end_ymd,
            },
            headers=headers,
        )
        activity_by_date: dict[date, dict[str, Any]] = {}
        for entry in activity_body.get("activities") or []:
            if not isinstance(entry, dict):
                continue
            day = _parse_ymd(entry.get("date"))
            if day is None:
                continue
            activity_by_date[day] = entry

        sleep_body = self._post_action(
            "/v2/sleep",
            data={
                "action": "getsummary",
                "startdateymd": start_ymd,
                "enddateymd": end_ymd,
            },
            headers=headers,
        )
        sleep_by_date: dict[date, dict[str, Any]] = {}
        for series in sleep_body.get("series") or []:
            if not isinstance(series, dict):
                continue
            day = _parse_ymd(series.get("date"))
            if day is None:
                continue
            sleep_by_date[day] = series

        all_dates = sorted(set(activity_by_date) | set(sleep_by_date))
        for day in all_dates:
            mapped = _map_daily(day, activity_by_date.get(day), sleep_by_date.get(day))
            outcome = self.upsert_daily_summary(session, mapped)
            result.records_processed += 1
            if outcome == "inserted":
                result.records_inserted += 1
            else:
                result.records_updated += 1
        log.info(
            "withings.daily.done",
            activity_dates=len(activity_by_date),
            sleep_dates=len(sleep_by_date),
            merged_dates=len(all_dates),
        )


def _parse_ymd(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _map_daily(
    day: date,
    activity: dict[str, Any] | None,
    sleep: dict[str, Any] | None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if activity is not None:
        raw["activity"] = activity
    if sleep is not None:
        raw["sleep"] = sleep

    steps: int | None = None
    if activity is not None:
        step_value = activity.get("steps")
        if isinstance(step_value, int | float):
            steps = int(step_value)

    sleep_score: int | None = None
    sleep_duration_seconds: int | None = None
    if sleep is not None:
        data = sleep.get("data")
        if isinstance(data, dict):
            score = data.get("sleep_score")
            if isinstance(score, int | float):
                sleep_score = int(score)
            light = _as_int(data.get("lightsleepduration"))
            deep = _as_int(data.get("deepsleepduration"))
            rem = _as_int(data.get("remsleepduration"))
            total = (light or 0) + (deep or 0) + (rem or 0)
            if total > 0:
                sleep_duration_seconds = total

    return {
        "date": day,
        "source": "withings",
        "sleep_score": sleep_score,
        "sleep_duration_seconds": sleep_duration_seconds,
        "steps": steps,
        "raw": raw,
    }


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None
