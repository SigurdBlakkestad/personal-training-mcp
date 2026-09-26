"""add activities.garmin_training_load

Revision ID: f1a6c3e9b284
Revises: e05c8d4a7b31
Create Date: 2026-09-26 12:00:00.000000

Garmin's own per-activity load (``activityTrainingLoad``, EPOC-based) is kept
as a reference column next to our computed ``training_load``. It is not used
for CTL/ATL — it is there so the two can be compared, e.g. when a new sensor
(Fenix 8 wrist running power) skews our number.

Backfill: the value already sits in the stored Garmin payload — ``raw`` for
Garmin-canonical rows, ``garmin_supplement`` for Strava rows a Garmin activity
was merged into. Only rows whose column is still NULL are touched, and only
when the JSON value is a number, so the cast can never fail.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'f1a6c3e9b284'
down_revision: str | Sequence[str] | None = 'e05c8d4a7b31'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('activities', sa.Column('garmin_training_load', sa.REAL(), nullable=True))
    op.execute(
        """
        UPDATE activities
        SET garmin_training_load = (
          (CASE WHEN source = 'garmin' THEN raw ELSE garmin_supplement END)
            ->> 'activityTrainingLoad'
        )::real
        WHERE garmin_training_load IS NULL
          AND jsonb_typeof(
            (CASE WHEN source = 'garmin' THEN raw ELSE garmin_supplement END)
              -> 'activityTrainingLoad'
          ) = 'number'
        """
    )

def downgrade() -> None:
    op.drop_column('activities', 'garmin_training_load')
