"""add service_credentials

Revision ID: d94b7c3e5f26
Revises: c82f6a1b3d04
Create Date: 2026-09-23 10:00:00.000000

Durable home for rotating auth state. Garmin's DI flow issues a new refresh
token on every refresh and invalidates the old one, so the static
GARMINTOKENS_B64 secret is single-use once the access token expires. RLS is
enabled to match c82f6a1b3d04 — this table holds credentials, so it must not be
the one public table left open.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd94b7c3e5f26'
down_revision: str | Sequence[str] | None = 'c82f6a1b3d04'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'service_credentials',
        sa.Column('service', sa.Text(), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('service'),
    )
    op.execute('ALTER TABLE public.service_credentials ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('service_credentials')
