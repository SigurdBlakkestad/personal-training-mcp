"""add strava-rich metrics columns

Revision ID: 9d2a3b4c5e11
Revises: 8c1f2a9b4e10
Create Date: 2026-05-15 15:00:00.000000

Promotes Strava activity fields out of raw JSONB onto first-class columns so
coaching tools can read them without parsing the raw payload. moving_time
matters more than elapsed_time for load (excludes stops), suffer_score is
Strava's own relative-effort proxy, is_trainer captures indoor/outdoor, and
kilojoules/max_watts/avg speed unlock cycling-specific analysis.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '9d2a3b4c5e11'
down_revision: str | Sequence[str] | None = '8c1f2a9b4e10'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('activities', sa.Column('moving_time_seconds', sa.Integer(), nullable=True))
    op.add_column('activities', sa.Column('suffer_score', sa.SmallInteger(), nullable=True))
    op.add_column('activities', sa.Column('is_trainer', sa.Boolean(), nullable=True))
    op.add_column('activities', sa.Column('kilojoules', sa.REAL(), nullable=True))
    op.add_column('activities', sa.Column('average_speed_ms', sa.REAL(), nullable=True))
    op.add_column('activities', sa.Column('workout_type', sa.SmallInteger(), nullable=True))
    op.add_column('activities', sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('activities', 'description')
    op.drop_column('activities', 'workout_type')
    op.drop_column('activities', 'average_speed_ms')
    op.drop_column('activities', 'kilojoules')
    op.drop_column('activities', 'is_trainer')
    op.drop_column('activities', 'suffer_score')
    op.drop_column('activities', 'moving_time_seconds')
