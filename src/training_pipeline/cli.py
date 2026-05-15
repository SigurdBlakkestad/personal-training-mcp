import argparse
import sys

from training_pipeline.derived.compute import recompute_all
from training_pipeline.ingestors.strava import StravaIngestor
from training_pipeline.notion_sync.runner import run_notion_mirror
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

    sub.add_parser(
        "notion-mirror",
        help="Mirror activities, current plan, and dashboard metrics into Notion",
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

    if args.command == "notion-mirror":
        mirror_result = run_notion_mirror()
        logger.info("cli.notion_mirror.complete", **mirror_result.to_dict())
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
