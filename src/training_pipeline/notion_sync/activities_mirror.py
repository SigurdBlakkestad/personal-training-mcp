"""Mirror recent activities (and their manual logs) into the Notion DB.

Idempotency: each activity row stores its Notion ``page_id`` back into
``activities.notion_page_id``. Subsequent runs hit ``update_page`` instead of
``create_page``. The mirror window is the last 30 days of ``start_time``.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from training_pipeline.notion_sync.client import NotionClient
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import Activity, ManualLog

logger = get_logger(__name__)

MIRROR_WINDOW_DAYS = 30
SPORT_TO_NOTION = {
    "cycling": "Cycling",
    "running": "Running",
    "swimming": "Swimming",
    "lifting": "Lifting",
    "walking": "Walking",
    "other": "Other",
}


def _latest_log_for(session: Session, activity_id: Any) -> ManualLog | None:
    return session.scalar(
        select(ManualLog)
        .where(ManualLog.activity_id == activity_id)
        .order_by(desc(ManualLog.logged_at))
        .limit(1)
    )


def _build_properties(activity: Activity, log: ManualLog | None) -> dict[str, Any]:
    title = activity.name or f"{activity.sport_type or 'session'} {activity.start_time.date()}"
    duration_min = (
        round(activity.duration_seconds / 60.0, 2)
        if activity.duration_seconds is not None
        else None
    )
    distance_km = (
        round(activity.distance_meters / 1000.0, 3)
        if activity.distance_meters is not None
        else None
    )
    sport_label = SPORT_TO_NOTION.get(activity.sport_type or "", "Other")

    props: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": title[:2000]}}]},
        "Date": {"date": {"start": activity.start_time.isoformat()}},
        "Sport": {"select": {"name": sport_label}},
    }
    if duration_min is not None:
        props["Duration (min)"] = {"number": duration_min}
    if distance_km is not None:
        props["Distance (km)"] = {"number": distance_km}
    if activity.avg_hr is not None:
        props["Avg HR"] = {"number": activity.avg_hr}
    if activity.training_load is not None:
        props["Training Load"] = {"number": round(float(activity.training_load), 2)}
    if log is not None:
        if log.rpe is not None:
            props["RPE"] = {"number": log.rpe}
        if log.pain_score is not None:
            props["Pain"] = {"number": log.pain_score}
        if log.notes is not None:
            props["Notes"] = {"rich_text": [{"text": {"content": log.notes[:2000]}}]}
    return props


def _fetch_recent_activities(session: Session) -> list[Activity]:
    cutoff = datetime.now(UTC) - timedelta(days=MIRROR_WINDOW_DAYS)
    return list(
        session.scalars(
            select(Activity)
            .where(Activity.start_time >= cutoff)
            .order_by(desc(Activity.start_time))
        )
    )


def mirror_activities(session: Session, client: NotionClient, database_id: str) -> dict[str, int]:
    activities = _fetch_recent_activities(session)
    created = 0
    updated = 0
    for activity in activities:
        log = _latest_log_for(session, activity.id)
        properties = _build_properties(activity, log)
        if activity.notion_page_id:
            client.update_page(activity.notion_page_id, properties=properties)
            updated += 1
        else:
            page = client.create_page(
                parent={"database_id": database_id},
                properties=properties,
            )
            page_id = page.get("id")
            if isinstance(page_id, str):
                activity.notion_page_id = page_id
            created += 1
    session.flush()
    logger.info(
        "notion.mirror.activities",
        processed=len(activities),
        created=created,
        updated=updated,
    )
    return {"processed": len(activities), "created": created, "updated": updated}
