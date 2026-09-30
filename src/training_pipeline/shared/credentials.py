"""Durable storage for rotating auth state in ``service_credentials``.

Garmin and Withings both invalidate the previous refresh token the moment they
issue a new one, so a stateless CI runner has to persist whatever it ends up
holding. The secret is only the seed for the first run.
"""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from training_pipeline.shared.db import get_session
from training_pipeline.shared.models import ServiceCredential


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
