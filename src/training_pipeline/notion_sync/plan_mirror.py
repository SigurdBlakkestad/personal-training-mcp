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

TABLE_HEADERS = ("Exercise", "Target", "Done reps", "Kg", "RPE", "Notes")


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


def _format_target(exercise: dict[str, Any]) -> str:
    sets = exercise.get("sets")
    reps = exercise.get("reps")
    weight = exercise.get("weight_kg")
    parts: list[str] = []
    if isinstance(sets, int) and isinstance(reps, int | str):
        parts.append(f"{sets}×{reps}")
    elif isinstance(sets, int):
        parts.append(f"{sets} sets")
    elif isinstance(reps, int | str):
        parts.append(f"{reps} reps")
    if isinstance(weight, int | float):
        weight_str = f"{weight:g}"
        parts.append(f"@{weight_str}kg")
    return " ".join(parts)


def _rich_text(content: str) -> list[dict[str, Any]]:
    if not content:
        return []
    return [{"type": "text", "text": {"content": content[:2000]}}]


def _paragraph(content: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(content)},
    }


def _heading_2(content: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": _rich_text(content)},
    }


def _table_row(values: list[str]) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "table_row",
        "table_row": {"cells": [_rich_text(v) for v in values]},
    }


def _exercises_table(exercises: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [_table_row(list(TABLE_HEADERS))]
    for ex in exercises:
        if not isinstance(ex, dict):
            continue
        name = str(ex.get("name") or "")
        notes = str(ex.get("notes") or "")
        rows.append(_table_row([name, _format_target(ex), "", "", "", notes]))
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": len(TABLE_HEADERS),
            "has_column_header": True,
            "has_row_header": False,
            "children": rows,
        },
    }


def _build_session_children(session_dict: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    description = str(session_dict.get("description") or "")
    if description:
        blocks.append(_paragraph(description))

    exercises = session_dict.get("exercises")
    if isinstance(exercises, list) and exercises:
        blocks.append(_heading_2("Exercises"))
        blocks.append(_exercises_table(exercises))

    blocks.append(_heading_2("Comments"))
    blocks.append(_paragraph(""))
    return blocks


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
        children = _build_session_children(session_dict)
        client.create_page(
            parent={"database_id": database_id},
            properties=properties,
            children=children,
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
