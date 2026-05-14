"""add athlete_context

Revision ID: 3737ebb86b2b
Revises: 0fc163bfd7ce
Create Date: 2026-05-14 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3737ebb86b2b'
down_revision: str | Sequence[str] | None = '0fc163bfd7ce'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'athlete_context',
        sa.Column('id', sa.SmallInteger(), server_default=sa.text('1'), nullable=False),
        sa.Column('ftp_watts', sa.Integer(), nullable=True),
        sa.Column('max_hr', sa.SmallInteger(), nullable=True),
        sa.Column('body_weight_kg', sa.REAL(), nullable=True),
        sa.Column('current_phase', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('id = 1', name='ck_athlete_context_singleton'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('athlete_context')
