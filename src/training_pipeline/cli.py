import argparse
import sys
from datetime import UTC, datetime
from datetime import date as date_type

from training_pipeline.calendar_publish.publisher import publish as publish_calendar
from training_pipeline.derived.compute import recompute_all
from training_pipeline.ingestors.garmin import GarminIngestor
from training_pipeline.ingestors.strava import StravaIngestor
from training_pipeline.ingestors.withings import WithingsIngestor
from training_pipeline.notion_sync.runner import run_notion_mirror
from training_pipeline.shared.db import get_session
from training_pipeline.shared.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(date_type.fromisoformat(value), datetime.min.time(), tzinfo=UTC)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="training_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Run an ingestor for a single source")
    sync.add_argument("--source", required=True, choices=["strava", "garmin", "withings"])
    sync.add_argument(
        "--since",
        default=None,
        help=(
            "ISO date (YYYY-MM-DD) to backfill from. Overrides the default 30-day "
            "lookback. For Garmin, this also keeps activity pagination going past "
            "rows already in Postgres so historical activities get re-mapped with "
            "the latest column extractions."
        ),
    )

    sub.add_parser(
        "compute-derived",
        help="Recompute TSS, CTL/ATL/TSB, weekly load, and weight trend metrics",
    )

    sub.add_parser(
        "notion-mirror",
        help="Mirror activities, current plan, and dashboard metrics into Notion",
    )

    sub.add_parser(
        "publish-calendar",
        help="Regenerate docs/training.ics from the current weekly plan(s)",
    )

    serve = sub.add_parser(
        "serve-mcp",
        help="Run the MCP server (FastAPI + SSE) locally",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging()

    if args.command == "sync":
        since = _parse_since(args.since)
        backfill = since is not None
        if args.source == "strava":
            result = StravaIngestor().run(since=since)
        elif args.source == "garmin":
            result = GarminIngestor(backfill=backfill).run(since=since)
        else:
            result = WithingsIngestor().run(since=since)
        logger.info(
            "cli.sync.complete",
            source=args.source,
            backfill=backfill,
            since=since.isoformat() if since else None,
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

    if args.command == "notion-mirror":
        mirror_result = run_notion_mirror()
        logger.info("cli.notion_mirror.complete", **mirror_result.to_dict())
        return 0

    if args.command == "publish-calendar":
        publish_result = publish_calendar()
        logger.info("cli.publish_calendar.complete", **publish_result.to_dict())
        return 0

    if args.command == "serve-mcp":
        import uvicorn

        logger.info("cli.serve_mcp.start", host=args.host, port=args.port)
        uvicorn.run(
            "training_pipeline.mcp_server.app:app",
            host=args.host,
            port=args.port,
            log_config=None,
        )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
