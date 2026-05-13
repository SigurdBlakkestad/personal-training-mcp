import argparse
import sys

from training_pipeline.derived.compute import recompute_all
from training_pipeline.ingestors.strava import StravaIngestor
from training_pipeline.shared.db import get_session
from training_pipeline.shared.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="training_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Run an ingestor for a single source")
    sync.add_argument("--source", required=True, choices=["strava"])

    sub.add_parser(
        "compute-derived",
        help="Recompute TSS, CTL/ATL/TSB, weekly load, and weight trend metrics",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging()

    if args.command == "sync" and args.source == "strava":
        result = StravaIngestor().run()
        logger.info(
            "cli.sync.complete",
            source="strava",
            records_processed=result.records_processed,
            records_inserted=result.records_inserted,
            records_updated=result.records_updated,
        )
        return 0

    if args.command == "compute-derived":
        with get_session() as session:
            counts = recompute_all(session)
        logger.info("cli.compute_derived.complete", **counts.to_dict())
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
