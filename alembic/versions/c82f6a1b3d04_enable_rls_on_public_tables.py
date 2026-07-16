"""enable row level security on all public tables

Revision ID: c82f6a1b3d04
Revises: b71d4e2f5a08
Create Date: 2026-07-15 09:00:00.000000

Supabase exposes every table in the ``public`` schema through PostgREST, and
its database linter raises an ERROR (``rls_disabled_in_public``) for any such
table without row level security. This pipeline never touches Postgres through
PostgREST — it connects directly as the table owner via ``DATABASE_URL``, and
the owner bypasses RLS entirely. So enabling RLS with no policies is exactly
right: the externally-exposed ``anon`` / ``authenticated`` roles get zero
access, while the pipeline keeps full access.

We use plain ``ENABLE ROW LEVEL SECURITY`` (not ``FORCE``) so the table owner
retains its bypass. ``alembic_version`` is included because Supabase flags it
too; the migration role owns it and is unaffected.

Idempotent: ``ENABLE``/``DISABLE ROW LEVEL SECURITY`` are no-ops when the flag
already has the target value.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c82f6a1b3d04"
down_revision: str | Sequence[str] | None = "b71d4e2f5a08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table Supabase flagged as public-without-RLS. alembic_version is
# Alembic's bookkeeping table; the rest are the pipeline's own tables.
_TABLES: tuple[str, ...] = (
    "alembic_version",
    "activities",
    "athlete_context",
    "body_measurements",
    "daily_summary",
    "derived_metrics",
    "ingestion_runs",
    "manual_logs",
    "weekly_plans",
)


def upgrade() -> None:
    # Each ALTER takes an ACCESS EXCLUSIVE lock. Fail fast on lock contention
    # (e.g. a leaked idle-in-transaction session) instead of blocking until the
    # statement timeout — a stuck migration is worse than a re-runnable one.
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
