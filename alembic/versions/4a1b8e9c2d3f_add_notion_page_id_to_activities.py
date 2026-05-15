"""add notion_page_id to activities

Revision ID: 4a1b8e9c2d3f
Revises: 3737ebb86b2b
Create Date: 2026-05-15 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4a1b8e9c2d3f'
down_revision: str | Sequence[str] | None = '3737ebb86b2b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'activities',
        sa.Column('notion_page_id', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('activities', 'notion_page_id')
