"""add garmin_supplement column to activities

Revision ID: a3e5c7d9f213
Revises: 9d2a3b4c5e11
Create Date: 2026-05-15 16:00:00.000000

Stashing the Garmin raw payload inside ``activities.raw`` as a ``garmin_supplement``
key did not survive subsequent Strava upserts — the upsert replaces ``raw``
wholesale. Promoting it to its own column lets the daily Strava sync update
``raw`` without touching the Garmin forensic dump.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = 'a3e5c7d9f213'
down_revision: str | Sequence[str] | None = '9d2a3b4c5e11'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'activities',
        sa.Column(
            'garmin_supplement',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    # Salvage any garmin_supplement values that the old in-raw nesting may
    # still have left behind, then drop the nested key. Safe to run when
    # neither condition holds.
    op.execute(
        """
        UPDATE activities
        SET garmin_supplement = raw->'garmin_supplement',
            raw = raw - 'garmin_supplement'
        WHERE raw ? 'garmin_supplement'
        """
    )


def downgrade() -> None:
    op.drop_column('activities', 'garmin_supplement')
