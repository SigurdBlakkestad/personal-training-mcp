"""Mirror the latest derived metrics into the Notion Dashboard page.

The Dashboard target is a single Notion page, not a multi-row database.
``NOTION_DB_METRICS_ID`` therefore holds a page ID. On each run we replace
the page's children with a fresh set of headings + paragraph blocks summarizing
training load, weekly volume, and weight trend.
"""

from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from training_pipeline.notion_sync.client import NotionClient
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import DerivedMetric

logger = get_logger(__name__)


def _latest_value(session: Session, metric_name: str) -> tuple[date_type, float] | None:
    row = session.execute(
        select(DerivedMetric.date, DerivedMetric.value)
        .where(DerivedMetric.metric_name == metric_name)
        .order_by(desc(DerivedMetric.date))
        .limit(1)
    ).first()
    if row is None:
        return None
    return row[0], float(row[1])


def _heading_block(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _paragraph_block(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _format_value(label: str, entry: tuple[date_type, float] | None, unit: str = "") -> str:
    if entry is None:
        return f"{label}: no data yet"
    when, value = entry
    return f"{label}: {value:.1f}{unit} (as of {when.isoformat()})"


def _build_blocks(session: Session) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date()
    week_start = today - timedelta(days=today.weekday())

    ctl = _latest_value(session, "ctl")
    atl = _latest_value(session, "atl")
    tsb = _latest_value(session, "tsb")
    cycling = _latest_value(session, "weekly_load_cycling")
    running = _latest_value(session, "weekly_load_running")
    lifting = _latest_value(session, "weekly_load_lifting")
    total_load = _latest_value(session, "weekly_load_total")
    weight_7d = _latest_value(session, "weight_7d_avg")
    weight_28d = _latest_value(session, "weight_28d_avg")

    header = f"Updated {today.isoformat()} (UTC). Week starting {week_start.isoformat()}."
    blocks: list[dict[str, Any]] = [
        _paragraph_block(header),
        _heading_block("Training Load"),
        _paragraph_block(_format_value("CTL (fitness, 42d EWMA)", ctl)),
        _paragraph_block(_format_value("ATL (fatigue, 7d EWMA)", atl)),
        _paragraph_block(_format_value("TSB (form = CTL - ATL)", tsb)),
        _heading_block("Weekly Volume"),
        _paragraph_block(_format_value("Cycling hours", cycling, " h")),
        _paragraph_block(_format_value("Running hours", running, " h")),
        _paragraph_block(_format_value("Lifting hours", lifting, " h")),
        _paragraph_block(_format_value("Total weekly load", total_load)),
        _heading_block("Weight Trend"),
        _paragraph_block(_format_value("7-day average", weight_7d, " kg")),
        _paragraph_block(_format_value("28-day average", weight_28d, " kg")),
    ]
    return blocks


def mirror_metrics(session: Session, client: NotionClient, page_id: str) -> dict[str, int]:
    existing = client.list_block_children(page_id)
    deleted = 0
    for block in existing:
        block_id = block.get("id")
        if isinstance(block_id, str):
            client.delete_block(block_id)
            deleted += 1

    blocks = _build_blocks(session)
    client.append_block_children(page_id, blocks)

    logger.info(
        "notion.mirror.metrics",
        page_id=page_id,
        deleted=deleted,
        appended=len(blocks),
    )
    return {"deleted": deleted, "appended": len(blocks)}
