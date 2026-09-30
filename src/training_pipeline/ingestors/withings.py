from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from training_pipeline.ingestors.base import IngestionResult, IngestorBase
from training_pipeline.ingestors.http import HttpClient
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.credentials import (
    load_service_credential,
    save_service_credential,
)
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import IngestionRun
from training_pipeline.shared.retry import is_connect_failure, is_retryable

logger = get_logger(__name__)

WITHINGS_API_BASE = "https://wbsapi.withings.net"
WITHINGS_CREDENTIAL_SERVICE = "withings"
WITHINGS_DEFAULT_LOOKBACK_DAYS = 30
# Subtracted from the last run's start before it becomes `lastupdate`, to cover
# Withings' own ingestion lag and clock skew. Re-fetched groups upsert idempotently.
WITHINGS_LASTUPDATE_OVERLAP = timedelta(hours=1)
# getmeas category 1 = real measurements (2 = user objectives/goals).
WITHINGS_MEASURE_CATEGORY_REAL = 1
WITHINGS_CREDENTIAL_SAVE_ATTEMPTS = 3
WITHINGS_CREDENTIAL_SAVE_BACKOFF_SECONDS = 0.5

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

        # The stored row is the live token; the secret only seeds the first run.
        initial_refresh = (
            load_service_credential(WITHINGS_CREDENTIAL_SERVICE) or settings.WITHINGS_REFRESH_TOKEN
        )
        access_token, new_refresh = self._refresh_access_token(
            client_id=settings.WITHINGS_CLIENT_ID,
            client_secret=settings.WITHINGS_CLIENT_SECRET,
            refresh_token=initial_refresh,
        )
        # Withings invalidates the old refresh token on every refresh, so persist
        # the new one now, outside the ingestion session: a later failure in
        # this run rolls that session back, and the rotation must survive it.
        self._save_refresh_token(new_refresh, log)
        log.info("withings.refresh_token.stored", rotated=new_refresh != initial_refresh)

        effective_since = since if since is not None else self._compute_since(session)
        now_utc = datetime.now(UTC)
        log.info("withings.fetch.start", since=effective_since.isoformat())

        result = IngestionResult()
        auth_headers = {"Authorization": f"Bearer {access_token}"}

        self._sync_body_measurements(session, auth_headers, effective_since, result, log)
        self._sync_daily(session, auth_headers, effective_since, now_utc, result, log)

        log.info(
            "withings.fetch.done",
            records_processed=result.records_processed,
            records_inserted=result.records_inserted,
            records_updated=result.records_updated,
        )
        return result

    def _compute_since(self, session: Session) -> datetime:
        latest = session.scalar(
            # started_at, not finished_at: the next run fetches by `lastupdate`,
            # so anything Withings received while this run was in flight must
            # still be newer than the cutoff.
            select(IngestionRun.started_at)
            .where(IngestionRun.source == "withings", IngestionRun.status == "success")
            .order_by(desc(IngestionRun.started_at))
            .limit(1)
        )
        if latest is None:
            return datetime.now(UTC) - timedelta(days=WITHINGS_DEFAULT_LOOKBACK_DAYS)
        return latest - WITHINGS_LASTUPDATE_OVERLAP

    def _save_refresh_token(self, refresh_token: str, log: Any) -> None:
        retryer = Retrying(
            stop=stop_after_attempt(WITHINGS_CREDENTIAL_SAVE_ATTEMPTS),
            wait=wait_exponential(multiplier=WITHINGS_CREDENTIAL_SAVE_BACKOFF_SECONDS),
            retry=retry_if_exception_type(SQLAlchemyError),
            reraise=True,
        )
        try:
            retryer(save_service_credential, WITHINGS_CREDENTIAL_SERVICE, refresh_token)
        except SQLAlchemyError:
            log.exception(
                "withings.refresh_token.save_failed",
                attempts=WITHINGS_CREDENTIAL_SAVE_ATTEMPTS,
                message=(
                    "Withings already consumed the previous refresh token and the new "
                    "one could not be stored; re-run scripts/withings_auth.py to re-authorize."
                ),
            )
            raise

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
            # The refresh rotates the token server-side: once the request may have
            # reached Withings, a replay would send the now-invalidated token and
            # lose the rotated one. Retry only if it provably never arrived.
            retry_on=is_connect_failure,
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
        retry_on: Callable[[BaseException], bool] = is_retryable,
    ) -> dict[str, Any]:
        response = self._http.post(path, data=data, headers=headers, retry_on=retry_on)
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
        result: IngestionResult,
        log: Any,
    ) -> None:
        # `lastupdate` filters on when Withings received the measurement, not
        # when it was taken, so a weigh-in that reaches the cloud after a run
        # (scale offline, late Wi-Fi sync) is still picked up by the next one.
        data: dict[str, Any] = {
            "action": "getmeas",
            "category": WITHINGS_MEASURE_CATEGORY_REAL,
            "lastupdate": int(since.timestamp()),
        }
        group_count = 0
        pages = 0
        while True:
            body = self._post_action("/measure", data=data, headers=headers)
            pages += 1
            groups = body.get("measuregrps") or []
            group_count += len(groups)
            for group in groups:
                if not isinstance(group, dict):
                    continue
                measurement = self._map_measure_group(group)
                if measurement is None:
                    log.warning(
                        "withings.body.group_skipped",
                        grpid=group.get("grpid"),
                        date=group.get("date"),
                    )
                    continue
                outcome = self.upsert_body_measurement(session, measurement)
                result.records_processed += 1
                if outcome == "inserted":
                    result.records_inserted += 1
                else:
                    result.records_updated += 1
            if not body.get("more"):
                break
            offset = body.get("offset")
            if not isinstance(offset, int) or offset <= data.get("offset", 0):
                raise WithingsAPIError(f"withings getmeas more=1 without a new offset: {offset!r}")
            data = {**data, "offset": offset}
        log.info("withings.body.done", groups=group_count, pages=pages)

    def _map_measure_group(self, group: dict[str, Any]) -> dict[str, Any] | None:
        epoch = group.get("date")
        grpid = group.get("grpid")
        # grpid is the upsert key; without it a re-fetch could not find its row.
        if epoch is None or not isinstance(grpid, int) or isinstance(grpid, bool):
            return None
        measured_at = datetime.fromtimestamp(int(epoch), tz=UTC)
        # Every mapped column is written, so a re-fetched group replaces the
        # stored row wholesale: a measure Withings dropped is cleared, not kept.
        mapped: dict[str, Any] = {
            "source": "withings",
            "source_id": str(grpid),
            "measured_at": measured_at,
            "raw": group,
            **dict.fromkeys(WITHINGS_MEASURE_TYPE_TO_COLUMN.values()),
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
