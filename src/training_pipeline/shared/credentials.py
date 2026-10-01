"""Durable storage for rotating auth state in ``service_credentials``.

Garmin and Withings both invalidate the previous refresh token the moment they
issue a new one, so a stateless CI runner has to persist whatever it ends up
holding. The secret is only the seed for the first run.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from training_pipeline.shared.db import get_engine, get_session
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import ServiceCredential

logger = get_logger(__name__)

# Short enough that a CI run waiting on a local backfill fails with
# LockNotAvailable well inside its step timeout (10 min Withings, 15 min
# Garmin) instead of being killed silently mid-wait.
SERVICE_CREDENTIAL_LOCK_TIMEOUT_SECONDS = 5 * 60


def load_service_credential(service: str) -> str | None:
    """Return the stored payload for ``service``, or None before the first run."""
    with get_session() as session:
        return session.scalar(
            select(ServiceCredential.payload).where(ServiceCredential.service == service)
        )


def save_service_credential(service: str, payload: str) -> None:
    """Upsert the payload for ``service`` in a transaction of its own.

    Deliberately not the ingestion session: a rotation that is rolled back with
    a failed sync leaves the next run replaying a dead token.
    """
    with get_session() as session:
        stmt = pg_insert(ServiceCredential).values(service=service, payload=payload)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["service"],
                set_={"payload": stmt.excluded.payload, "updated_at": func.now()},
            )
        )


@contextmanager
def service_credential_lock(
    service: str, *, timeout_seconds: int = SERVICE_CREDENTIAL_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Hold an exclusive Postgres advisory lock on ``service``'s stored token.

    Every load → refresh → persist cycle runs inside this, from CI and local
    runs alike: two holders refreshing the same token means one of them stores
    a token the other has already invalidated. The workflows' concurrency
    groups only serialize GitHub runs of one workflow; this covers the rest.

    The lock is session-level because it spans HTTP calls, so it lives on a
    dedicated autocommit connection (no transaction left idle in between) and
    Postgres drops it if that connection dies. That needs a direct or
    session-mode connection: Supabase's transaction pooler (port 6543) would
    hand the unlock to a different backend. Waiting longer than
    ``timeout_seconds`` raises ``sqlalchemy.exc.OperationalError``.
    """
    key = f"service_credentials:{service}"
    engine = get_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # statement_timeout too: Supabase's default (2 min) would otherwise
        # cancel the wait first. Kept above lock_timeout so that the clearer
        # LockNotAvailable is what a wait that runs out reports.
        conn.execute(
            text(
                "SELECT set_config('lock_timeout', :lock, false),"
                " set_config('statement_timeout', :statement, false)"
            ),
            {"lock": f"{timeout_seconds}s", "statement": f"{timeout_seconds + 30}s"},
        )
        try:
            conn.execute(text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), {"key": key})
        finally:
            # The connection goes back to the pool; don't leak the timeouts.
            # A dead connection has nothing to reset, and trying would mask
            # the error that killed it.
            if not conn.invalidated:
                conn.execute(text("RESET lock_timeout"))
                conn.execute(text("RESET statement_timeout"))
        try:
            yield
        finally:
            _release_advisory_lock(conn, key)


def _release_advisory_lock(conn: Connection, key: str) -> None:
    """Unlock without letting a failed unlock fail the caller.

    The body may have completed a sync whose session has not committed yet,
    or raised the error worth reporting; neither should give way to an unlock
    on a connection that dropped (which already released the lock). If the
    unlock fails on a live connection, close it rather than return a lock
    holder to the pool.
    """
    try:
        conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": key})
    except SQLAlchemyError:
        logger.warning("service_credential_lock.unlock_failed", key=key, exc_info=True)
        conn.invalidate()
