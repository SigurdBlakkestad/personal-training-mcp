"""add body_measurements.source_id and unique (source, source_id)

Revision ID: a7c4e1f9b352
Revises: 0b7d2e5f8a61
Create Date: 2026-09-30 12:00:00.000000

The old upsert matched on (source, measured_at, weight_kg), comparing the REAL
column against a float8 parameter; Postgres widens the stored value, so the
match failed and every re-fetch of a Withings group inserted a duplicate. The
key is now the Withings measure-group id (``grpid``), upserted with
ON CONFLICT against a unique constraint.

Backfill: ``source_id`` is read from ``raw->>'grpid'`` (``raw`` is the whole
measure group) when it is a plain non-negative integer. Dedupe then keeps the
latest ingested row per (source, source_id), or per (source, measured_at) for
rows whose payload had no grpid, before the constraint is added.

Downgrade drops the constraint and the column; the deleted duplicates are not
restored.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'a7c4e1f9b352'
down_revision: str | Sequence[str] | None = '0b7d2e5f8a61'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('body_measurements', sa.Column('source_id', sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE body_measurements
        SET source_id = raw ->> 'grpid'
        WHERE source = 'withings'
          AND jsonb_typeof(raw -> 'grpid') = 'number'
          AND (raw ->> 'grpid') ~ '^[0-9]+$'
        """
    )
    op.execute(
        """
        WITH ranked AS (
          SELECT
            id,
            row_number() OVER (
              PARTITION BY
                source,
                source_id,
                CASE WHEN source_id IS NULL THEN measured_at END
              ORDER BY ingested_at DESC, id DESC
            ) AS rn
          FROM body_measurements
        )
        DELETE FROM body_measurements b
        USING ranked r
        WHERE b.id = r.id AND r.rn > 1
        """
    )
    op.create_unique_constraint(
        'uq_body_measurements_source_source_id',
        'body_measurements',
        ['source', 'source_id'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_body_measurements_source_source_id', 'body_measurements', type_='unique'
    )
    op.drop_column('body_measurements', 'source_id')
