import logging
import os
import sys
from typing import cast

import structlog
from structlog.stdlib import BoundLogger
from structlog.types import Processor

from training_pipeline.shared.config import get_settings


def _is_ci() -> bool:
    return os.getenv("CI", "").lower() in {"1", "true", "yes"} or "GITHUB_ACTIONS" in os.environ


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Processor
    if _is_ci():
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> BoundLogger:
    if name is None:
        return cast(BoundLogger, structlog.get_logger())
    return cast(BoundLogger, structlog.get_logger(name))
