"""add garmin-rich metrics columns

Revision ID: 8c1f2a9b4e10
Revises: 4a1b8e9c2d3f
Create Date: 2026-05-15 14:00:00.000000

Adds coach-facing metrics that Garmin returns but the original schema only
captured inside the raw JSONB. Promoting them to columns makes the data
indexable, queryable, and trivially exposed through the MCP tools without
asking Claude to dig into ``raw``.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '8c1f2a9b4e10'
down_revision: str | Sequence[str] | None = '4a1b8e9c2d3f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('activities', sa.Column('aerobic_training_effect', sa.REAL(), nullable=True))
    op.add_column('activities', sa.Column('anaerobic_training_effect', sa.REAL(), nullable=True))
    op.add_column('activities', sa.Column('training_effect_label', sa.Text(), nullable=True))
    op.add_column('activities', sa.Column('vo2_max', sa.REAL(), nullable=True))
    op.add_column('activities', sa.Column('moderate_intensity_minutes', sa.Integer(), nullable=True))
    op.add_column('activities', sa.Column('vigorous_intensity_minutes', sa.Integer(), nullable=True))
    op.add_column('activities', sa.Column('min_hr', sa.SmallInteger(), nullable=True))
    op.add_column('activities', sa.Column('max_power', sa.SmallInteger(), nullable=True))
    op.add_column('activities', sa.Column('avg_stride_length_cm', sa.REAL(), nullable=True))
    op.add_column('activities', sa.Column('avg_ground_contact_time_ms', sa.Integer(), nullable=True))

    op.add_column('daily_summary', sa.Column('training_readiness_score', sa.SmallInteger(), nullable=True))
    op.add_column('daily_summary', sa.Column('training_readiness_level', sa.Text(), nullable=True))
    op.add_column('daily_summary', sa.Column('vo2_max_running', sa.REAL(), nullable=True))
    op.add_column('daily_summary', sa.Column('vo2_max_cycling', sa.REAL(), nullable=True))
    op.add_column('daily_summary', sa.Column('intensity_minutes_moderate', sa.Integer(), nullable=True))
    op.add_column('daily_summary', sa.Column('intensity_minutes_vigorous', sa.Integer(), nullable=True))
    op.add_column('daily_summary', sa.Column('respiration_avg', sa.REAL(), nullable=True))
    op.add_column('daily_summary', sa.Column('stress_max', sa.SmallInteger(), nullable=True))
    op.add_column('daily_summary', sa.Column('active_calories', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('daily_summary', 'active_calories')
    op.drop_column('daily_summary', 'stress_max')
    op.drop_column('daily_summary', 'respiration_avg')
    op.drop_column('daily_summary', 'intensity_minutes_vigorous')
    op.drop_column('daily_summary', 'intensity_minutes_moderate')
    op.drop_column('daily_summary', 'vo2_max_cycling')
    op.drop_column('daily_summary', 'vo2_max_running')
    op.drop_column('daily_summary', 'training_readiness_level')
    op.drop_column('daily_summary', 'training_readiness_score')

    op.drop_column('activities', 'avg_ground_contact_time_ms')
    op.drop_column('activities', 'avg_stride_length_cm')
    op.drop_column('activities', 'max_power')
    op.drop_column('activities', 'min_hr')
    op.drop_column('activities', 'vigorous_intensity_minutes')
    op.drop_column('activities', 'moderate_intensity_minutes')
    op.drop_column('activities', 'vo2_max')
    op.drop_column('activities', 'training_effect_label')
    op.drop_column('activities', 'anaerobic_training_effect')
    op.drop_column('activities', 'aerobic_training_effect')
