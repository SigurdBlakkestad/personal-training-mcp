from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from training_pipeline.shared import credentials


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[MagicMock]:
    """Each get_session() call yields a fresh mock session, recorded in order."""
    opened: list[MagicMock] = []

    @contextmanager
    def fake_get_session() -> Iterator[MagicMock]:
        session = MagicMock(spec=Session)
        session.scalar.return_value = "stored"
        opened.append(session)
        yield session

    monkeypatch.setattr(credentials, "get_session", fake_get_session)
    return opened


def test_load_reads_payload_in_its_own_session(sessions: list[MagicMock]) -> None:
    assert credentials.load_service_credential("withings") == "stored"

    assert len(sessions) == 1
    stmt = sessions[0].scalar.call_args.args[0]
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "FROM service_credentials" in compiled
    assert "service_credentials.service = 'withings'" in compiled


def test_save_upserts_on_service_in_its_own_session(sessions: list[MagicMock]) -> None:
    credentials.save_service_credential("withings", "new-token")

    assert len(sessions) == 1
    stmt = sessions[0].execute.call_args.args[0]
    compiled = " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())
    assert compiled.startswith("INSERT INTO service_credentials (service, payload)")
    assert (
        "ON CONFLICT (service) DO UPDATE SET payload = excluded.payload, updated_at = now()"
        in compiled
    )
    assert stmt.compile(dialect=postgresql.dialect()).params == {
        "service": "withings",
        "payload": "new-token",
    }
