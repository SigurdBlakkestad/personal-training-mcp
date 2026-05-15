"""Mirror the current weekly plan into the Notion Plan database.

On each run, delete any previous "Planned" rows for the same week (archive
them in Notion terms) and replace them with the sessions from the latest
``weekly_plans`` row where ``is_current = true``. Status defaults to
"Planned" — completed/skipped flips happen in Notion and are not synced back.
"""

from datetime import date as date_type
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from training_pipeline.notion_sync.client import NotionClient
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import WeeklyPlan

logger = get_logger(__name__)

SESSION_TYPE_OPTIONS = {"Cycling", "Lifting", "Mobility", "Rest", "Other"}
INTENSITY_OPTIONS = {"Easy", "Moderate", "Hard"}


def _normalize_session_type(raw: Any) -> str:
    if not isinstance(raw, str):
        return "Other"
    candidate = raw.strip().title()
    return candidate if candidate in SESSION_TYPE_OPTIONS else "Other"


def _normalize_intensity(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().title()
    return candidate if candidate in INTENSITY_OPTIONS else None


def _build_session_properties(week_of: date_type, session_dict: dict[str, Any]) -> dict[str, Any]:
    session_date_raw = session_dict.get("date")
    if isinstance(session_date_raw, str):
        session_date = session_date_raw
    elif isinstance(session_date_raw, date_type):
        session_date = session_date_raw.isoformat()
    else:
        session_date = week_of.isoformat()

    title = str(session_dict.get("description") or session_dict.get("session_type") or "Session")
    description = str(session_dict.get("description") or "")
    duration = session_dict.get("duration_min")

    session_type = _normalize_session_type(session_dict.get("session_type"))
    props: dict[str, Any] = {
        "Session": {"title": [{"text": {"content": title[:2000]}}]},
        "Date": {"date": {"start": session_date}},
        "Session Type": {"select": {"name": session_type}},
        "Status": {"select": {"name": "Planned"}},
    }
    if description:
        props["Description"] = {"rich_text": [{"text": {"content": description[:2000]}}]}
    if isinstance(duration, int | float):
        props["Duration (min)"] = {"number": float(duration)}
    intensity = _normalize_intensity(session_dict.get("intensity"))
    if intensity is not None:
        props["Intensity"] = {"select": {"name": intensity}}
    return props


def _current_plan(session: Session) -> WeeklyPlan | None:
    return session.scalar(
        select(WeeklyPlan)
        .where(WeeklyPlan.is_current.is_(True))
        .order_by(desc(WeeklyPlan.week_of), desc(WeeklyPlan.version))
        .limit(1)
    )


def _previous_planned_pages(
    client: NotionClient, database_id: str, week_of: date_type
) -> list[dict[str, Any]]:
    week_start = week_of.isoformat()
    # ISO week is 7 days starting Monday.
    week_end_date = date_type.fromordinal(week_of.toordinal() + 6).isoformat()
    return client.query_database(
        database_id,
        filter={
            "and": [
                {"property": "Status", "select": {"equals": "Planned"}},
                {"property": "Date", "date": {"on_or_after": week_start}},
                {"property": "Date", "date": {"on_or_before": week_end_date}},
            ]
        },
    )


def mirror_plan(session: Session, client: NotionClient, database_id: str) -> dict[str, int]:
    plan = _current_plan(session)
    if plan is None:
        logger.info("notion.mirror.plan.no_current_plan")
        return {"sessions": 0, "archived": 0, "created": 0}

    sessions = plan.plan if isinstance(plan.plan, list) else []
    previous = _previous_planned_pages(client, database_id, plan.week_of)
    archived = 0
    for page in previous:
        page_id = page.get("id")
        if isinstance(page_id, str):
            client.update_page(page_id, archived=True)
            archived += 1

    created = 0
    for session_dict in sessions:
        if not isinstance(session_dict, dict):
            continue
        properties = _build_session_properties(plan.week_of, session_dict)
        client.create_page(
            parent={"database_id": database_id},
            properties=properties,
        )
        created += 1

    logger.info(
        "notion.mirror.plan",
        week_of=plan.week_of.isoformat(),
        version=plan.version,
        sessions=len(sessions),
        archived=archived,
        created=created,
    )
    return {"sessions": len(sessions), "archived": archived, "created": created}
