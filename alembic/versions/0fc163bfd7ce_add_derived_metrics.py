"""add derived_metrics

Revision ID: 0fc163bfd7ce
Revises: 6e2668dce523
Create Date: 2026-05-13 17:19:21.946008

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0fc163bfd7ce'
down_revision: str | Sequence[str] | None = '6e2668dce523'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'derived_metrics',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('metric_name', sa.Text(), nullable=False),
        sa.Column('value', sa.REAL(), nullable=False),
        sa.Column(
            'computed_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('date', 'metric_name', name='uq_derived_metrics_date_metric'),
    )
    op.create_index(
        "ix_derived_metrics_metric_name_date_desc",
        "derived_metrics",
        ["metric_name", sa.text("date DESC")],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_derived_metrics_metric_name_date_desc", table_name="derived_metrics")
    op.drop_table('derived_metrics')
