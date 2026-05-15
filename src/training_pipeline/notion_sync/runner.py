"""Entrypoint that runs all three Notion mirrors inside a single DB session."""

from dataclasses import dataclass, field
from typing import Any

from training_pipeline.notion_sync.activities_mirror import mirror_activities
from training_pipeline.notion_sync.client import NotionClient
from training_pipeline.notion_sync.metrics_mirror import mirror_metrics
from training_pipeline.notion_sync.plan_mirror import mirror_plan
from training_pipeline.shared.config import get_settings
from training_pipeline.shared.db import get_session
from training_pipeline.shared.logging import get_logger

logger = get_logger(__name__)


@dataclass
class NotionMirrorResult:
    activities: dict[str, int] = field(default_factory=dict)
    plan: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"activities": self.activities, "plan": self.plan, "metrics": self.metrics}


def run_notion_mirror() -> NotionMirrorResult:
    settings = get_settings()
    if not settings.NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN is required to run the Notion mirror")
    client = NotionClient(settings.NOTION_TOKEN)
    result = NotionMirrorResult()
    with get_session() as session:
        if settings.NOTION_DB_ACTIVITIES_ID:
            result.activities = mirror_activities(session, client, settings.NOTION_DB_ACTIVITIES_ID)
        else:
            logger.warning("notion.mirror.activities.skipped", reason="missing_database_id")
        if settings.NOTION_DB_PLAN_ID:
            result.plan = mirror_plan(session, client, settings.NOTION_DB_PLAN_ID)
        else:
            logger.warning("notion.mirror.plan.skipped", reason="missing_database_id")
        if settings.NOTION_DB_METRICS_ID:
            result.metrics = mirror_metrics(session, client, settings.NOTION_DB_METRICS_ID)
        else:
            logger.warning("notion.mirror.metrics.skipped", reason="missing_page_id")
    logger.info("notion.mirror.complete", **result.to_dict())
    return result
