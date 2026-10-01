from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

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


@pytest.fixture
def lock_conn(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The dedicated connection service_credential_lock opens on the engine."""
    engine = MagicMock()
    conn = engine.connect.return_value.execution_options.return_value.__enter__.return_value
    conn.invalidated = False
    monkeypatch.setattr(credentials, "get_engine", lambda: engine)
    return conn


def _statements(conn: MagicMock) -> list[tuple[str, dict[str, Any] | None]]:
    return [
        (str(c.args[0]), c.args[1] if len(c.args) > 1 else None)
        for c in conn.execute.call_args_list
    ]


def test_lock_holds_advisory_lock_for_the_block(lock_conn: MagicMock) -> None:
    key = {"key": "service_credentials:garmin"}

    with credentials.service_credential_lock("garmin", timeout_seconds=90):
        held = _statements(lock_conn)

    assert held == [
        (
            "SELECT set_config('lock_timeout', :lock, false),"
            " set_config('statement_timeout', :statement, false)",
            # Regression: Supabase's 2 min statement_timeout cancelled any
            # longer wait before lock_timeout could fire.
            {"lock": "90s", "statement": "120s"},
        ),
        ("SELECT pg_advisory_lock(hashtextextended(:key, 0))", key),
        ("RESET lock_timeout", None),
        ("RESET statement_timeout", None),
    ]
    assert _statements(lock_conn)[4:] == [
        ("SELECT pg_advisory_unlock(hashtextextended(:key, 0))", key)
    ]


def test_failed_unlock_does_not_fail_the_block(lock_conn: MagicMock) -> None:
    """Regression: an unlock on a connection that dropped mid-sync raised out
    of the block, rolling back a sync that had succeeded."""

    def execute(stmt: Any, params: dict[str, Any] | None = None) -> None:
        if "pg_advisory_unlock" in str(stmt):
            raise OperationalError("unlock", params, Exception("server closed the connection"))

    lock_conn.execute.side_effect = execute

    with capture_logs() as logs:
        with credentials.service_credential_lock("garmin"):
            pass

    assert [e["event"] for e in logs] == ["service_credential_lock.unlock_failed"]
    # Never return a possible lock holder to the pool.
    lock_conn.invalidate.assert_called_once_with()


def test_dead_connection_skips_the_timeout_reset(lock_conn: MagicMock) -> None:
    def execute(stmt: Any, params: dict[str, Any] | None = None) -> None:
        if "pg_advisory_lock" in str(stmt):
            lock_conn.invalidated = True
            raise OperationalError("lock", params, Exception("server closed the connection"))

    lock_conn.execute.side_effect = execute

    with pytest.raises(OperationalError, match="server closed the connection"):
        with credentials.service_credential_lock("garmin"):
            pass

    assert not any("RESET" in sql for sql, _ in _statements(lock_conn))


def test_lock_releases_when_the_block_raises(lock_conn: MagicMock) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with credentials.service_credential_lock("withings"):
            raise RuntimeError("boom")

    assert _statements(lock_conn)[-1] == (
        "SELECT pg_advisory_unlock(hashtextextended(:key, 0))",
        {"key": "service_credentials:withings"},
    )


def test_lock_timeout_resets_and_never_runs_the_block(lock_conn: MagicMock) -> None:
    def execute(stmt: Any, params: dict[str, Any] | None = None) -> None:
        if "pg_advisory_lock" in str(stmt):
            raise OperationalError("lock", params, Exception("lock timeout"))

    lock_conn.execute.side_effect = execute
    ran = False

    with pytest.raises(OperationalError):
        with credentials.service_credential_lock("garmin"):
            ran = True

    assert not ran
    statements = [sql for sql, _ in _statements(lock_conn)]
    assert statements[-2:] == ["RESET lock_timeout", "RESET statement_timeout"]
    assert not any("unlock" in sql for sql in statements)
