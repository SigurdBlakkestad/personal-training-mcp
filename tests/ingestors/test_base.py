from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from training_pipeline.ingestors.base import IngestionResult, IngestorBase
from training_pipeline.shared.models import BodyMeasurement, IngestionRun


class StubSession:
    """Minimal in-memory stand-in for a SQLAlchemy Session."""

    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    def add(self, obj: Any) -> None:
        if isinstance(obj, IngestionRun) and obj.id is None:
            obj.id = uuid4()
            self.store["runs"][obj.id] = obj

    def flush(self) -> None:
        pass

    def get(self, model: type, pk: Any) -> Any:
        if model is IngestionRun:
            return self.store["runs"].get(pk)
        return None

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeIngestor(IngestorBase):
    def __init__(self, result: IngestionResult, *, fail: bool = False) -> None:
        self._result = result
        self._fail = fail
        self.sync_called_with: tuple[Any, datetime | None] | None = None

    @property
    def name(self) -> str:
        return "Fake"

    @property
    def source_key(self) -> str:
        return "fake"

    def _sync(self, session: Session, since: datetime | None) -> IngestionResult:
        self.sync_called_with = (session, since)
        if self._fail:
            raise RuntimeError("boom")
        return self._result


@pytest.fixture
def session_store() -> dict[str, Any]:
    return {"runs": {}}


@pytest.fixture
def patched_get_session(
    monkeypatch: pytest.MonkeyPatch, session_store: dict[str, Any]
) -> list[StubSession]:
    sessions: list[StubSession] = []

    @contextmanager
    def fake_get_session():  # type: ignore[no-untyped-def]
        s = StubSession(session_store)
        sessions.append(s)
        yield s

    monkeypatch.setattr("training_pipeline.ingestors.base.get_session", fake_get_session)
    return sessions


def test_run_creates_and_finalizes_ingestion_run(
    patched_get_session: list[StubSession], session_store: dict[str, Any]
) -> None:
    result = IngestionResult(
        records_processed=10, records_inserted=7, records_updated=3, cursor="abc"
    )
    ingestor = FakeIngestor(result)

    returned = ingestor.run()

    # Three sessions: _open_run, _sync, _close_run
    assert len(patched_get_session) == 3

    runs = session_store["runs"]
    assert len(runs) == 1
    run = next(iter(runs.values()))
    assert run.source == "fake"
    assert run.status == "success"
    assert run.records_processed == 10
    assert run.records_inserted == 7
    assert run.records_updated == 3
    assert run.cursor == "abc"
    assert run.finished_at is not None
    assert run.error is None
    assert returned is result


def test_run_marks_failed_on_exception(
    patched_get_session: list[StubSession], session_store: dict[str, Any]
) -> None:
    ingestor = FakeIngestor(IngestionResult(), fail=True)

    with pytest.raises(RuntimeError, match="boom"):
        ingestor.run()

    runs = session_store["runs"]
    assert len(runs) == 1
    run = next(iter(runs.values()))
    assert run.status == "failed"
    assert run.error == "boom"
    assert run.finished_at is not None
    # Counts/cursor are only written on success; left untouched on failure.
    assert run.cursor is None


def test_run_passes_since_to_sync(
    patched_get_session: list[StubSession],
) -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    ingestor = FakeIngestor(IngestionResult())
    ingestor.run(since=since)
    assert ingestor.sync_called_with is not None
    assert ingestor.sync_called_with[1] == since


def test_upsert_activity_inserted() -> None:
    ingestor = FakeIngestor(IngestionResult())
    session = MagicMock(spec=Session)
    session.execute.return_value.scalar_one.return_value = True

    outcome = ingestor.upsert_activity(
        session,
        {
            "source": "strava",
            "source_id": "123",
            "start_time": datetime(2026, 1, 1, tzinfo=UTC),
            "raw": {"foo": "bar"},
        },
    )
    assert outcome == "inserted"
    session.execute.assert_called_once()


def test_upsert_activity_updated() -> None:
    ingestor = FakeIngestor(IngestionResult())
    session = MagicMock(spec=Session)
    session.execute.return_value.scalar_one.return_value = False

    outcome = ingestor.upsert_activity(
        session,
        {
            "source": "strava",
            "source_id": "123",
            "start_time": datetime(2026, 1, 1, tzinfo=UTC),
            "raw": {},
        },
    )
    assert outcome == "updated"


def test_upsert_body_measurement_inserts_when_no_match() -> None:
    ingestor = FakeIngestor(IngestionResult())
    session = MagicMock(spec=Session)
    session.scalar.return_value = None

    outcome = ingestor.upsert_body_measurement(
        session,
        {
            "source": "withings",
            "measured_at": datetime(2026, 1, 1, tzinfo=UTC),
            "weight_kg": 80.0,
            "raw": {},
        },
    )
    assert outcome == "inserted"
    assert session.add.call_count == 1
    added = session.add.call_args.args[0]
    assert isinstance(added, BodyMeasurement)


def test_upsert_body_measurement_returns_updated_when_match() -> None:
    ingestor = FakeIngestor(IngestionResult())
    session = MagicMock(spec=Session)
    session.scalar.return_value = MagicMock(spec=BodyMeasurement)

    outcome = ingestor.upsert_body_measurement(
        session,
        {
            "source": "withings",
            "measured_at": datetime(2026, 1, 1, tzinfo=UTC),
            "weight_kg": 80.0,
            "raw": {},
        },
    )
    assert outcome == "updated"
    session.add.assert_not_called()


def test_upsert_daily_summary_inserted() -> None:
    ingestor = FakeIngestor(IngestionResult())
    session = MagicMock(spec=Session)
    session.execute.return_value.scalar_one.return_value = True

    outcome = ingestor.upsert_daily_summary(
        session,
        {
            "date": date(2026, 1, 1),
            "source": "withings",
            "sleep_score": 80,
            "raw": {},
        },
    )
    assert outcome == "inserted"
    session.execute.assert_called_once()
