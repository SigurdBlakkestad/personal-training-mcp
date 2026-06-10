"""Backfill Strava activities over a bounded date window.

Run locally when the daily sync missed activities (e.g. late uploads whose
``start_date`` predates the cursor advance):

    python scripts/backfill_strava.py --after 2026-06-01
    python scripts/backfill_strava.py --after 2026-06-01 --before 2026-06-10

The script reuses ``StravaIngestor`` so the upsert path is identical to the
scheduled sync. Activities are idempotent on ``(source, source_id)`` — re-runs
over an already-synced window will update existing rows, not duplicate them.
``DATABASE_URL`` and the Strava OAuth env vars must be set (typically via
``.env`` for local runs against Supabase prod).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from datetime import date as date_type

from training_pipeline.ingestors.strava import StravaIngestor
from training_pipeline.shared.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _parse_iso_date(value: str) -> datetime:
    return datetime.combine(date_type.fromisoformat(value), datetime.min.time(), tzinfo=UTC)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_strava",
        description="Backfill Strava activities over a bounded date window.",
    )
    parser.add_argument(
        "--after",
        required=True,
        help="ISO date (YYYY-MM-DD). Activities with start_date on or after this date are fetched.",
    )
    parser.add_argument(
        "--before",
        default=None,
        help="ISO date (YYYY-MM-DD), exclusive upper bound. Omit for 'up to now'.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging()

    after_dt = _parse_iso_date(args.after)
    before_dt = _parse_iso_date(args.before) if args.before else None
    if before_dt is not None and before_dt <= after_dt:
        parser.error("--before must be strictly later than --after")

    logger.info(
        "backfill_strava.start",
        after=after_dt.isoformat(),
        before=before_dt.isoformat() if before_dt else None,
    )

    result = StravaIngestor(before=before_dt).run(since=after_dt)

    logger.info(
        "backfill_strava.complete",
        records_processed=result.records_processed,
        records_inserted=result.records_inserted,
        records_updated=result.records_updated,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
