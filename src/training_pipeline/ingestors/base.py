from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from training_pipeline.shared.db import get_session
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import (
    Activity,
    BodyMeasurement,
    DailySummary,
    IngestionRun,
)

logger = get_logger(__name__)

UpsertOutcome = Literal["inserted", "updated"]


@dataclass
class IngestionResult:
    records_processed: int = 0
    records_inserted: int = 0
    records_updated: int = 0
    cursor: str | None = None


class IngestorBase(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def source_key(self) -> str: ...

    @abstractmethod
    def _sync(self, session: Session, since: datetime | None) -> IngestionResult: ...

    def run(self, since: datetime | None = None) -> IngestionResult:
        log = logger.bind(source=self.source_key)
        run_id = self._open_run()
        log = log.bind(ingestion_run_id=str(run_id))
        log.info("ingestion.start", since=since.isoformat() if since else None)

        try:
            with get_session() as session:
                result = self._sync(session, since)
        except Exception as exc:
            log.exception("ingestion.failed", error=str(exc))
            self._close_run(run_id, status="failed", error=str(exc), result=None)
            raise

        self._close_run(run_id, status="success", error=None, result=result)
        log.info(
            "ingestion.success",
            records_processed=result.records_processed,
            records_inserted=result.records_inserted,
            records_updated=result.records_updated,
        )
        return result

    def _open_run(self) -> UUID:
        with get_session() as session:
            row = IngestionRun(
                source=self.source_key,
                started_at=datetime.now(UTC),
                status="running",
            )
            session.add(row)
            session.flush()
            return row.id

    def _close_run(
        self,
        run_id: UUID,
        *,
        status: str,
        error: str | None,
        result: IngestionResult | None,
    ) -> None:
        try:
            with get_session() as session:
                row = session.get(IngestionRun, run_id)
                if row is None:
                    logger.warning(
                        "ingestion.finalize.missing",
                        source=self.source_key,
                        ingestion_run_id=str(run_id),
                    )
                    return
                row.finished_at = datetime.now(UTC)
                row.status = status
                row.error = error
                if result is not None:
                    row.records_processed = result.records_processed
                    row.records_inserted = result.records_inserted
                    row.records_updated = result.records_updated
                    row.cursor = result.cursor
        except Exception:
            logger.exception(
                "ingestion.finalize.error",
                source=self.source_key,
                ingestion_run_id=str(run_id),
            )

    def upsert_activity(self, session: Session, activity: dict[str, Any]) -> UpsertOutcome:
        insert_stmt = pg_insert(Activity).values(**activity)
        update_cols: dict[str, Any] = {
            col: insert_stmt.excluded[col] for col in activity if col not in ("source", "source_id")
        }
        update_cols["updated_at"] = func.now()
        stmt: Any = insert_stmt.on_conflict_do_update(
            constraint="uq_activities_source_source_id",
            set_=update_cols,
        ).returning(literal_column("(xmax = 0)").label("inserted"))
        inserted = bool(session.execute(stmt).scalar_one())
        return "inserted" if inserted else "updated"

    def upsert_body_measurement(
        self, session: Session, measurement: dict[str, Any]
    ) -> UpsertOutcome:
        # TODO: revisit dedup. body_measurements has no perfect natural key; matching on
        # (source, measured_at, weight_kg) holds for Withings but may need rework once
        # Garmin lands.
        weight = measurement.get("weight_kg")
        weight_cond = (
            BodyMeasurement.weight_kg.is_(None)
            if weight is None
            else BodyMeasurement.weight_kg == weight
        )
        existing = session.scalar(
            select(BodyMeasurement).where(
                BodyMeasurement.source == measurement["source"],
                BodyMeasurement.measured_at == measurement["measured_at"],
                weight_cond,
            )
        )
        if existing is not None:
            return "updated"
        session.add(BodyMeasurement(**measurement))
        return "inserted"

    def upsert_daily_summary(self, session: Session, summary: dict[str, Any]) -> UpsertOutcome:
        insert_stmt = pg_insert(DailySummary).values(**summary)
        update_cols: dict[str, Any] = {
            col: insert_stmt.excluded[col] for col in summary if col not in ("date", "source")
        }
        stmt: Any = insert_stmt.on_conflict_do_update(
            constraint="uq_daily_summary_date_source",
            set_=update_cols,
        ).returning(literal_column("(xmax = 0)").label("inserted"))
        inserted = bool(session.execute(stmt).scalar_one())
        return "inserted" if inserted else "updated"
