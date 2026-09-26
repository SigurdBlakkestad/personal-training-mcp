"""add activities.device_id

Revision ID: 0b7d2e5f8a61
Revises: f1a6c3e9b284
Create Date: 2026-09-26 12:30:00.000000

Records which Garmin device captured each activity (e.g. 3644296614 for the
Fenix 8, 3341217191 for the FR945), so a change in sensors can be seen next to
a change in the numbers it produces.

Backfill: Garmin's top-level ``deviceId`` is read from the stored payload —
``raw`` for Garmin-canonical rows, ``garmin_supplement`` for Strava rows a
Garmin activity was merged into. Only rows whose column is still NULL are
touched, and only when the value is a plain non-negative integer (at most 18
digits), so the bigint cast can never fail.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0b7d2e5f8a61'
down_revision: str | Sequence[str] | None = 'f1a6c3e9b284'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('activities', sa.Column('device_id', sa.BigInteger(), nullable=True))
    op.execute(
        """
        UPDATE activities
        SET device_id = (
          (CASE WHEN source = 'garmin' THEN raw ELSE garmin_supplement END)
            ->> 'deviceId'
        )::bigint
        WHERE device_id IS NULL
          AND (
            (CASE WHEN source = 'garmin' THEN raw ELSE garmin_supplement END)
              ->> 'deviceId'
          ) ~ '^[0-9]{1,18}$'
        """
    )


def downgrade() -> None:
    op.drop_column('activities', 'device_id')
