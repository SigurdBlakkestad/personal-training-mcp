from __future__ import annotations

import base64
import io
import os
import re
import tarfile
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from garminconnect import GarminConnectAuthenticationError
from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session
from structlog.stdlib import BoundLogger

from training_pipeline.ingestors.base import IngestionResult, IngestorBase
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.credentials import (
    load_service_credential,
    save_service_credential,
    service_credential_lock,
)
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import (
    Activity,
    ActivityExerciseSet,
    ActivityLap,
    IngestionRun,
)

logger = get_logger(__name__)

GARMIN_CREDENTIAL_SERVICE = "garmin"
GARMIN_DEFAULT_LOOKBACK_DAYS = 30
GARMIN_PAGE_SIZE = 20
# The activity feed is ordered by start time, but a workout reaches Garmin
# Connect only when the watch syncs — possibly hours after a run has already
# passed its start time. Re-scan this far behind the last successful run so
# late uploads are still picked up; already-stored activities are skipped.
GARMIN_OVERLAP_MARGIN_HOURS = 72
# HTTP statuses meaning Garmin rejected the token. These never mean "no data
# for this day" and must fail the run like a dead login would.
GARMIN_AUTH_FAILURE_STATUSES: frozenset[int] = frozenset({401, 403})
# garminconnect reports an endpoint's HTTP status only inside the message,
# e.g. "API call client error (403): API Error 403".
_API_ERROR_STATUS_RE = re.compile(r"API Error (\d{3})")
STRAVA_DEDUPE_WINDOW_SECONDS = 60

# Sports whose laps are worth storing, on top of the hasIntensityIntervals
# gate. Endurance sessions have laps too, but with intensityType null they are
# odometer marks, not workout shape. Structured runs are the obvious next
# entry here; cycling is where the per-lap watts are.
LAP_DETAIL_SPORTS: frozenset[str] = frozenset({"cycling"})

# Columns Garmin alone provides — when a Garmin activity merges into an
# existing Strava row, copy these onto the Strava row so the rich data is not
# trapped inside ``garmin_supplement``.
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

# Columns where Garmin is the source of truth whenever it recorded the
# activity. Most are device measurements — Strava receives the same .fit
# file but re-processes it (no auto-pause for indoor rides, lower-HR pause
# samples folded into the average, etc.), producing values that distort
# training intensity. ``name`` is also Garmin-priority because activities
# are deliberately named on the watch ("Sykkeløkt (3)") whereas Strava
# auto-generates generic labels ("Afternoon Ride"). When both sources cover
# the same session, Garmin's value wins for these fields.
# ``garmin_training_load`` is Garmin's own per-activity load (EPOC-based),
# kept for reference only — CTL/ATL use our computed ``training_load``.
# ``device_id`` is the Garmin device that recorded the session.
GARMIN_PRIORITY_FIELDS: tuple[str, ...] = (
    "name",
    "duration_seconds",
    "avg_hr",
    "max_hr",
    "distance_meters",
    "elevation_gain_meters",
    "avg_power",
    "max_power",
    "normalized_power",
    "avg_cadence",
    "calories",
    "garmin_training_load",
    "device_id",
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


def _parse_laps(splits: Any) -> list[dict[str, Any]]:
    """Map Garmin's ``/splits`` response to activity_laps rows.

    Garmin returns laps under ``lapDTOs`` with a 1-based ``lapIndex`` and an
    ``intensityType`` of WARMUP / ACTIVE / RECOVERY / COOLDOWN. Note the
    per-lap cadence key is ``averageBikeCadence``, not the session-level
    ``averageBikingCadenceInRevPerMinute``.
    """
    if not isinstance(splits, dict):
        return []
    laps = splits.get("lapDTOs")
    if not isinstance(laps, list):
        return []

    rows: list[dict[str, Any]] = []
    for position, lap in enumerate(laps, start=1):
        if not isinstance(lap, dict):
            continue
        index = _coerce_int(lap.get("lapIndex"))
        rows.append(
            {
                "lap_index": index if index is not None else position,
                "lap_type": _coerce_str(lap.get("intensityType")),
                "duration_s": _coerce_float(lap.get("duration")),
                "moving_duration_s": _coerce_float(lap.get("movingDuration")),
                "distance_meters": _coerce_float(lap.get("distance")),
                "avg_power": _coerce_int(lap.get("averagePower")),
                "max_power": _coerce_int(lap.get("maxPower")),
                "normalized_power": _coerce_int(lap.get("normalizedPower")),
                "avg_hr": _coerce_int(lap.get("averageHR")),
                "max_hr": _coerce_int(lap.get("maxHR")),
                "avg_cadence": _coerce_int(
                    lap.get("averageBikeCadence")
                    if lap.get("averageBikeCadence") is not None
                    else lap.get("averageRunCadence")
                ),
            }
        )
    return rows


def _top_exercise(exercises: Any) -> tuple[str | None, float | None]:
    """Return (name, confidence) for the most likely detected movement.

    Garmin does not record what the lifter chose — the watch classifies the
    motion and answers with a ranked candidate list. Highest probability wins,
    and a tie goes to a named candidate because "UNKNOWN, equally likely"
    carries no information. ``UNKNOWN`` is deliberately kept when it genuinely
    leads: real sets come back as ``UNKNOWN`` at 99.6% alongside named
    candidates at 0%, and reporting the 0% guess would invent a movement the
    watch never claimed.

    The label is ``CATEGORY/SUB`` when a sub-category is given (Garmin usually
    leaves it null), otherwise just the category.
    """
    if not isinstance(exercises, list):
        return None, None
    ranked = [e for e in exercises if isinstance(e, dict)]
    if not ranked:
        return None, None
    best = max(
        ranked,
        key=lambda e: (
            _coerce_float(e.get("probability")) or 0.0,
            e.get("category") not in (None, "UNKNOWN"),
        ),
    )
    category = _coerce_str(best.get("category"))
    sub = _coerce_str(best.get("name"))
    if category is None:
        return None, None
    label = f"{category}/{sub}" if sub else category
    return label, _coerce_float(best.get("probability"))


def _parse_exercise_sets(payload: Any) -> list[dict[str, Any]]:
    """Map Garmin's ``/exerciseSets`` response to activity_exercise_sets rows.

    ``weight`` is grams when present. It is null on every set the watch
    records on its own — Garmin has no way to measure load, so a value only
    appears once one is typed into Garmin Connect. REST entries are kept:
    rest length is half of what makes a strength session readable.
    """
    if not isinstance(payload, dict):
        return []
    sets = payload.get("exerciseSets")
    if not isinstance(sets, list):
        return []

    rows: list[dict[str, Any]] = []
    for position, entry in enumerate(sets):
        if not isinstance(entry, dict):
            continue
        index = _coerce_int(entry.get("messageIndex"))
        name, confidence = _top_exercise(entry.get("exercises"))
        weight_grams = _coerce_float(entry.get("weight"))
        rows.append(
            {
                "set_index": index if index is not None else position,
                "set_type": _coerce_str(entry.get("setType")),
                "exercise_name": name,
                "exercise_confidence": confidence,
                "reps": _coerce_int(entry.get("repetitionCount")),
                # Grams, and 0 is Garmin's "no load recorded" placeholder
                # rather than a real bodyweight set.
                "weight_kg": weight_grams / 1000.0 if weight_grams else None,
                "duration_s": _coerce_float(entry.get("duration")),
            }
        )
    return rows


def decode_tokens_to_dir(b64: str) -> str:
    raw = base64.b64decode(b64)
    tmp_dir = tempfile.mkdtemp(prefix="garmin-tokens-")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(tmp_dir, filter="data")
    nested = os.path.join(tmp_dir, ".garminconnect")
    if os.path.isdir(nested):
        return nested
    return tmp_dir


def load_stored_tokens() -> str | None:
    """Return the persisted tokenstore payload, or None before the first run."""
    return load_service_credential(GARMIN_CREDENTIAL_SERVICE)


def save_stored_tokens(payload: str) -> None:
    """Upsert the tokenstore payload in a transaction of its own.

    Deliberately not the ingestion session: Garmin invalidates the previous
    refresh token the moment it issues a new one, so a rotation that is rolled
    back with a failed sync leaves the next run replaying a dead token.
    """
    save_service_credential(GARMIN_CREDENTIAL_SERVICE, payload)


def _serialize_tokens(client: Any) -> str | None:
    """Serialize the client's current tokens, including any rotation that
    happened during login or a mid-sync refresh.

    Returns None for injected doubles: the ``client``/``client_factory`` seams
    accept any object, and a MagicMock answers ``client.dumps()`` with another
    mock rather than a payload worth storing.
    """
    inner = getattr(client, "client", None)
    dumps = getattr(inner, "dumps", None)
    if dumps is None:
        return None
    payload = dumps()
    return payload if isinstance(payload, str) else None


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


def _is_auth_failure(exc: Exception) -> bool:
    """True when Garmin rejected the token rather than lacking data.

    garminconnect raises ``GarminConnectAuthenticationError`` for 401s but
    surfaces a 403 as a generic connection error, with the status on the
    attached response when there is one and otherwise only in the message.
    """
    if isinstance(exc, GarminConnectAuthenticationError):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if not isinstance(status, int):
        match = _API_ERROR_STATUS_RE.search(str(exc))
        status = int(match.group(1)) if match else None
    return status in GARMIN_AUTH_FAILURE_STATUSES


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
        self._persisted_tokens: str | None = None

    @property
    def name(self) -> str:
        return "Garmin"

    @property
    def source_key(self) -> str:
        return "garmin"

    def _sync(self, session: Session, since: datetime | None) -> IngestionResult:
        # The whole sync, not just login: garminconnect refreshes (and so
        # rotates) the token whenever it is about to expire, at any request.
        with service_credential_lock(GARMIN_CREDENTIAL_SERVICE):
            return self._sync_locked(session, since)

    def _sync_locked(self, session: Session, since: datetime | None) -> IngestionResult:
        log = logger.bind(source="garmin")

        client = self._client if self._client is not None else self._initialize_client()
        effective_since = since if since is not None else self._compute_since(session)
        log.info("garmin.fetch.start", since=effective_since.isoformat())

        result = IngestionResult()
        try:
            self._sync_activities(client, session, effective_since, result, log)
            self._sync_daily_summaries(client, session, effective_since, result, log)
        finally:
            # A stale access token is refreshed mid-sync too, rotating the
            # refresh token again. Persist whatever the client ended up holding,
            # including when the sync itself failed.
            self._persist_tokens(client, log)

        log.info(
            "garmin.fetch.done",
            records_processed=result.records_processed,
            records_inserted=result.records_inserted,
            records_updated=result.records_updated,
        )
        return result

    def _initialize_client(self) -> Any:
        tokenstore = self._load_tokenstore()
        if self._client_factory is not None:
            return self._client_factory(tokenstore)

        from garminconnect import Garmin

        client = Garmin()
        self._persist_on_refresh(client)
        client.login(tokenstore=tokenstore)
        # login() refreshes when the access token is stale, and that refresh
        # already rotated the token Garmin will accept next time. Store it
        # before the sync gets a chance to fail.
        self._persist_tokens(client, logger.bind(source="garmin"))
        return client

    def _persist_on_refresh(self, client: Any) -> None:
        """Store the token the moment garminconnect rotates it.

        Garmin invalidates the previous refresh token on every refresh, and a
        refresh can happen at any request. Waiting for the ``finally`` in
        ``_sync_locked`` would lose the rotation to a SIGKILL or a lost runner.
        """
        inner = client.client
        refresh = inner._refresh_di_token
        log = logger.bind(source="garmin")

        def refresh_and_persist() -> None:
            refresh()
            try:
                self._persist_tokens(client, log)
            except Exception:
                # garminconnect swallows anything raised here (logging it at
                # DEBUG), so log it ourselves; the token stays in memory and
                # the ``finally`` in ``_sync_locked`` tries again.
                log.exception("garmin.tokens.persist_on_refresh_failed")

        inner._refresh_di_token = refresh_and_persist

    def _load_tokenstore(self) -> str:
        """Return a tokenstore garminconnect accepts.

        ``login`` takes either inline JSON or a filesystem path, so the stored
        payload goes straight through without ever touching disk. The
        GARMINTOKENS_B64 secret is only the seed for the first run — once a
        token has been stored it is the source of truth, because the secret
        still holds whatever Garmin has since invalidated.
        """
        stored = load_stored_tokens()
        if stored:
            self._persisted_tokens = stored
            return stored

        settings = get_settings()
        if not settings.GARMINTOKENS_B64:
            raise RuntimeError(
                "No stored Garmin tokens and GARMINTOKENS_B64 is not set. "
                "Run scripts/garmin_auth.py locally first."
            )
        return decode_tokens_to_dir(settings.GARMINTOKENS_B64)

    def _persist_tokens(self, client: Any, log: BoundLogger) -> None:
        payload = _serialize_tokens(client)
        if payload is None or payload == self._persisted_tokens:
            return
        save_stored_tokens(payload)
        self._persisted_tokens = payload
        log.info("garmin.tokens.persisted")

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
        # Backfill re-processes everything from an explicit ``since``, so it
        # needs no overlap; a scheduled run re-scans the overlap window.
        cutoff = since if self._backfill else since - timedelta(hours=GARMIN_OVERLAP_MARGIN_HOURS)
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
                if start_time < cutoff:
                    stop = True
                    break
                source_id = str(activity["activityId"])
                # A late upload can sit behind newer, already-stored
                # activities, so a stored one is skipped rather than ending
                # the scan.
                if not self._backfill and self._activity_exists(session, source_id):
                    continue
                mapped = self._map_activity(activity, start_time)
                merged = self._merge_into_strava_if_exists(session, mapped, log)
                if merged is not None:
                    result.records_processed += 1
                    result.records_updated += 1
                    self._sync_activity_detail(client, session, activity, mapped, merged, log)
                    continue
                outcome = self.upsert_activity(session, mapped)
                result.records_processed += 1
                if outcome == "inserted":
                    result.records_inserted += 1
                else:
                    result.records_updated += 1
                self._sync_activity_detail(client, session, activity, mapped, None, log)
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
    ) -> Activity | None:
        """Fold a Garmin activity into an overlapping Strava row.

        Returns the Strava row that absorbed it, so the caller can hang lap and
        exercise-set detail off the same id, or None when there is no match.
        """
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
            return None
        strava.garmin_supplement = garmin_mapped["raw"]
        for field in GARMIN_PRIORITY_FIELDS:
            value = garmin_mapped.get(field)
            if value is not None:
                setattr(strava, field, value)
        for field in GARMIN_ONLY_ACTIVITY_FIELDS:
            value = garmin_mapped.get(field)
            if value is not None and getattr(strava, field, None) is None:
                setattr(strava, field, value)
        log.info(
            "garmin.activity.merged_into_strava",
            strava_source_id=strava.source_id,
            garmin_source_id=garmin_mapped["source_id"],
        )
        return strava

    def _sync_activity_detail(
        self,
        client: Any,
        session: Session,
        activity: dict[str, Any],
        mapped: dict[str, Any],
        row: Activity | None,
        log: BoundLogger,
    ) -> None:
        """Store the per-lap / per-set breakdown behind one activity.

        Each endpoint is one extra request per activity, so both are gated on
        flags from the activity list rather than fetched blindly, and both go
        through ``_safe_call``: a detail endpoint that 404s must cost its own
        activity's breakdown and nothing more.
        """
        wants_laps = mapped["sport_type"] in LAP_DETAIL_SPORTS and bool(
            activity.get("hasIntensityIntervals")
        )
        wants_sets = mapped["sport_type"] == "lifting"
        if not wants_laps and not wants_sets:
            return

        activity_row = row if row is not None else self._find_activity(session, mapped)
        if activity_row is None:
            log.warning("garmin.activity.detail_row_missing", source_id=mapped["source_id"])
            return

        source_id = mapped["source_id"]
        detail_log = log.bind(garmin_activity_id=source_id)

        if wants_laps:
            splits = self._safe_call(
                client.get_activity_splits, source_id, detail_log, "activity_splits"
            )
            laps = _parse_laps(splits)
            if laps:
                self._replace_children(session, ActivityLap, activity_row.id, laps)
                detail_log.info("garmin.activity.laps_stored", count=len(laps))

        if wants_sets:
            payload = self._safe_call(
                client.get_activity_exercise_sets, source_id, detail_log, "exercise_sets"
            )
            sets = _parse_exercise_sets(payload)
            if sets:
                self._replace_children(session, ActivityExerciseSet, activity_row.id, sets)
                detail_log.info(
                    "garmin.activity.exercise_sets_stored",
                    count=len(sets),
                    with_weight=sum(1 for entry in sets if entry["weight_kg"] is not None),
                )

    def _find_activity(self, session: Session, mapped: dict[str, Any]) -> Activity | None:
        return session.scalar(
            select(Activity).where(
                Activity.source == mapped["source"],
                Activity.source_id == mapped["source_id"],
            )
        )

    def _replace_children(
        self,
        session: Session,
        model: type[ActivityLap] | type[ActivityExerciseSet],
        activity_id: UUID,
        rows: list[dict[str, Any]],
    ) -> None:
        """Delete-then-insert the child rows for one activity.

        Upserting on (activity_id, index) would leave orphans behind whenever
        Garmin drops a lap — which happens when an activity is edited in
        Connect. Replacing wholesale keeps the stored breakdown equal to what
        Garmin currently reports, which is what idempotent means here.
        """
        session.execute(delete(model).where(model.activity_id == activity_id))
        session.add_all([model(activity_id=activity_id, **row) for row in rows])

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
            "garmin_training_load": _coerce_float(activity.get("activityTrainingLoad")),
            "device_id": _coerce_int(activity.get("deviceId")),
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
        fetchers: dict[str, Callable[[str], Any]] = {
            "user_summary": client.get_user_summary,
            "sleep_data": client.get_sleep_data,
            "hrv_data": client.get_hrv_data,
            "training_readiness": client.get_training_readiness,
            "max_metrics": client.get_max_metrics,
            "intensity_minutes": client.get_intensity_minutes_data,
            "respiration": client.get_respiration_data,
        }
        failures: Counter[str] = Counter()
        days = 0
        while cursor <= today:
            iso = cursor.isoformat()
            days += 1
            payloads = {
                endpoint: self._safe_call(fetch, iso, log, endpoint, failures)
                for endpoint, fetch in fetchers.items()
            }
            if all(v is None for v in payloads.values()):
                cursor += timedelta(days=1)
                continue
            user_summary = payloads["user_summary"]
            sleep = payloads["sleep_data"]
            hrv = payloads["hrv_data"]
            readiness = payloads["training_readiness"]
            max_metrics = payloads["max_metrics"]
            intensity = payloads["intensity_minutes"]
            respiration = payloads["respiration"]
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

        if not failures:
            return
        # A quiet day still has a user summary; every endpoint failing on
        # every day means the session or the API is broken.
        if all(failures[endpoint] == days for endpoint in fetchers):
            # Keep the activities stored earlier in this run: the error is
            # there to fail the run, not to discard what did sync.
            session.commit()
            raise RuntimeError(
                f"Every Garmin daily endpoint failed on all {days} day(s) since "
                f"{since.date().isoformat()}; see garmin.endpoint_failed warnings."
            )
        log.warning("garmin.daily.partial_failures", days=days, failures=dict(failures))

    def _safe_call(
        self,
        func: Callable[[str], Any],
        arg: str,
        log: BoundLogger,
        endpoint: str,
        failures: Counter[str] | None = None,
    ) -> Any:
        """Call one Garmin endpoint, trading a failure for None.

        Garmin throws on endpoints that simply hold no data — a day with no
        HRV reading, an activity with no splits. Used for both the daily
        endpoints (``arg`` is an ISO date) and the per-activity detail ones
        (``arg`` is the Garmin activity id); either way one dead endpoint must
        not take the whole sync down with it. A rejected token is the
        exception: it is re-raised so the run fails loud. ``failures``, when
        given, counts swallowed failures per endpoint.
        """
        try:
            return func(arg)
        except Exception as exc:  # noqa: BLE001 -- Garmin endpoints throw on missing data
            if _is_auth_failure(exc):
                raise
            log.warning(
                "garmin.endpoint_failed",
                endpoint=endpoint,
                arg=arg,
                error=str(exc),
            )
            if failures is not None:
                failures[endpoint] += 1
            return None
