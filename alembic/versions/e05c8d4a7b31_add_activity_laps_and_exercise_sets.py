"""add activity_laps and activity_exercise_sets

Revision ID: e05c8d4a7b31
Revises: d94b7c3e5f26
Create Date: 2026-09-23 12:00:00.000000

Session-level averages hide the shape of a workout. A 4x8 interval ride and a
steady endurance ride of the same duration and average power are the same row
in ``activities``; only the per-lap splits tell them apart. Likewise a strength
session collapses to duration and calories unless the per-set reps are stored.

Both tables hang off ``activities`` with ON DELETE CASCADE and are keyed on
(activity_id, index) so a re-sync of the same activity is idempotent, matching
the (source, source_id) guarantee the ingestors already make.

RLS is enabled to match c82f6a1b3d04 — every public table carries it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e05c8d4a7b31'
down_revision: str | Sequence[str] | None = 'd94b7c3e5f26'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'activity_laps',
        sa.Column(
            'id',
            sa.UUID(),
            server_default=sa.text('gen_random_uuid()'),
            nullable=False,
        ),
        sa.Column('activity_id', sa.UUID(), nullable=False),
        sa.Column('lap_index', sa.Integer(), nullable=False),
        sa.Column('lap_type', sa.Text(), nullable=True),
        sa.Column('duration_s', sa.REAL(), nullable=True),
        sa.Column('moving_duration_s', sa.REAL(), nullable=True),
        sa.Column('distance_meters', sa.REAL(), nullable=True),
        sa.Column('avg_power', sa.SmallInteger(), nullable=True),
        sa.Column('max_power', sa.SmallInteger(), nullable=True),
        sa.Column('normalized_power', sa.SmallInteger(), nullable=True),
        sa.Column('avg_hr', sa.SmallInteger(), nullable=True),
        sa.Column('max_hr', sa.SmallInteger(), nullable=True),
        sa.Column('avg_cadence', sa.SmallInteger(), nullable=True),
        sa.Column(
            'ingested_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['activity_id'], ['activities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('activity_id', 'lap_index', name='uq_activity_laps_activity_lap'),
    )
    op.create_index('ix_activity_laps_activity_id', 'activity_laps', ['activity_id'])

    op.create_table(
        'activity_exercise_sets',
        sa.Column(
            'id',
            sa.UUID(),
            server_default=sa.text('gen_random_uuid()'),
            nullable=False,
        ),
        sa.Column('activity_id', sa.UUID(), nullable=False),
        sa.Column('set_index', sa.Integer(), nullable=False),
        sa.Column('set_type', sa.Text(), nullable=True),
        sa.Column('exercise_name', sa.Text(), nullable=True),
        # Garmin auto-detects the movement and returns a ranked candidate list
        # rather than a label. ``exercise_name`` is the top candidate and this
        # is its confidence, so a coach can tell "bench press" from "the watch
        # guessed bench press at 59%".
        sa.Column('exercise_confidence', sa.REAL(), nullable=True),
        sa.Column('reps', sa.SmallInteger(), nullable=True),
        sa.Column('weight_kg', sa.REAL(), nullable=True),
        sa.Column('duration_s', sa.REAL(), nullable=True),
        sa.Column(
            'ingested_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['activity_id'], ['activities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'activity_id', 'set_index', name='uq_activity_exercise_sets_activity_set'
        ),
    )
    op.create_index(
        'ix_activity_exercise_sets_activity_id', 'activity_exercise_sets', ['activity_id']
    )

    op.execute('ALTER TABLE public.activity_laps ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public.activity_exercise_sets ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_activity_exercise_sets_activity_id', table_name='activity_exercise_sets')
    op.drop_table('activity_exercise_sets')
    op.drop_index('ix_activity_laps_activity_id', table_name='activity_laps')
    op.drop_table('activity_laps')
