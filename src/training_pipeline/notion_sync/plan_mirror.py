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

# Normalized session-type vocabulary. Maps raw input strings (from the MCP
# save_weekly_plan tool, which Claude sets freely) into the canonical labels
# that show up as Notion select options. ``Strength`` is a common synonym for
# ``Lifting`` in this codebase's plans, so it collapses to ``Lifting``.
SESSION_TYPE_ALIASES: dict[str, str] = {
    "cycling": "Cycling",
    "running": "Running",
    "swimming": "Swimming",
    "lifting": "Lifting",
    "strength": "Lifting",
    "weights": "Lifting",
    "mobility": "Mobility",
    "walking": "Walking",
    "walk": "Walking",
    "rest": "Rest",
    "off": "Rest",
}

# Emoji icon per canonical session type — gives Notion calendar tiles and
# gallery cards a glanceable indicator without colour-coding the title text.
SESSION_ICONS: dict[str, str] = {
    "Cycling": "🚴",
    "Running": "🏃",
    "Swimming": "🏊",
    "Lifting": "🏋️",
    "Walking": "🚶",
    "Mobility": "🧘",
    "Rest": "💤",
    "Other": "📌",
}

INTENSITY_OPTIONS = {"Easy", "Moderate", "Hard"}

TABLE_HEADERS = ("Exercise", "Target", "Done reps", "Kg", "RPE", "Notes")
# Notion rich_text limit per item — content longer than this must be split
# across multiple text objects within the same block.
RICH_TEXT_CHUNK = 1900


def _normalize_session_type(raw: Any) -> str:
    if not isinstance(raw, str):
        return "Other"
    key = raw.strip().lower()
    if not key:
        return "Other"
    return SESSION_TYPE_ALIASES.get(key, "Other")


def _normalize_intensity(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().title()
    return candidate if candidate in INTENSITY_OPTIONS else None


def _resolve_title(session_dict: dict[str, Any], session_type: str) -> str:
    """Find a calendar-tile-friendly title. Prefer the explicit ``title`` field
    (what the MCP tool docstring asks for); fall back to the first description
    line; finally to the canonical session type. Never falls back to the full
    multi-line description — that's what caused titles to start with
    'WARM-UP (8 min): ...'.
    """
    explicit = session_dict.get("title")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    desc = session_dict.get("description")
    if isinstance(desc, str):
        first_line = desc.strip().splitlines()[0].strip() if desc.strip() else ""
        if first_line and len(first_line) <= 80:
            return first_line
    return session_type


def _build_session_properties(week_of: date_type, session_dict: dict[str, Any]) -> dict[str, Any]:
    session_date_raw = session_dict.get("date")
    if isinstance(session_date_raw, str):
        session_date = session_date_raw
    elif isinstance(session_date_raw, date_type):
        session_date = session_date_raw.isoformat()
    else:
        session_date = week_of.isoformat()

    session_type = _normalize_session_type(session_dict.get("session_type"))
    title = _resolve_title(session_dict, session_type)
    duration = session_dict.get("duration_min")

    props: dict[str, Any] = {
        "Session": {"title": [{"text": {"content": title[:2000]}}]},
        "Date": {"date": {"start": session_date}},
        "Session Type": {"select": {"name": session_type}},
        "Status": {"select": {"name": "Planned"}},
    }
    # Description is intentionally NOT mirrored into a database property — the
    # full text lives in the page body where Notion can render line breaks and
    # the user is not forced to read the same content twice.
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


def _is_section_header(line: str) -> bool:
    """Detect lines like 'WARM-UP (8 min):' or 'MAIN:' so they can render as
    bold sub-headers rather than plain paragraphs. Heuristic: the part before
    the first ':' is short, and once parenthetical context (e.g. ``(8 min)``)
    is stripped, what remains is uppercase letters / digits / hyphens /
    spaces — i.e. it looks like a label, not a sentence."""
    if ":" not in line:
        return False
    head, _, _ = line.partition(":")
    head = head.strip()
    if not head or len(head) > 40:
        return False
    bare_chars: list[str] = []
    depth = 0
    for ch in head:
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            if depth > 0:
                depth -= 1
            continue
        if depth == 0:
            bare_chars.append(ch)
    bare = "".join(bare_chars).strip()
    if not bare:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZÆØÅ0123456789 /-")
    return all(ch in allowed for ch in bare) and any(ch.isalpha() for ch in bare)


def _paragraph(content: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(content)},
    }


def _bold_paragraph(content: str) -> dict[str, Any]:
    """Paragraph rendered with bold annotation — used for section headers
    extracted from the description ('WARM-UP', 'MAIN', etc.)."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": content[:2000]},
                    "annotations": {"bold": True},
                }
            ]
        },
    }


def _heading_2(content: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": _rich_text(content)},
    }


def _description_blocks(description: str) -> list[dict[str, Any]]:
    """Render the description's multi-line content as one paragraph per line.

    Lines that look like section headers (``WARM-UP (8 min):``, ``MAIN:`` …)
    become bold paragraphs so the structure is scannable in the page body.
    Empty lines collapse — Notion's paragraph spacing handles the visual
    grouping on its own.
    """
    if not description.strip():
        return []
    blocks: list[dict[str, Any]] = []
    for raw_line in description.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _is_section_header(line):
            blocks.append(_bold_paragraph(line))
        else:
            blocks.append(_paragraph(line))
    return blocks


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
    blocks.extend(_description_blocks(description))

    exercises = session_dict.get("exercises")
    if isinstance(exercises, list) and exercises:
        blocks.append(_heading_2("Exercises"))
        blocks.append(_exercises_table(exercises))

    blocks.append(_heading_2("Notes"))
    blocks.append(_paragraph(""))
    return blocks


def _current_plans(session: Session) -> list[WeeklyPlan]:
    """Return every weekly plan marked is_current=True, ordered by week.

    The schema lets multiple weeks each carry an is_current=True row (one per
    week). Mirroring "the" current plan is a misnomer — every flagged week
    needs to land in Notion, otherwise saving a plan for week N+1 silently
    hides week N from the mirror.
    """
    return list(
        session.scalars(
            select(WeeklyPlan)
            .where(WeeklyPlan.is_current.is_(True))
            .order_by(WeeklyPlan.week_of, desc(WeeklyPlan.version))
        )
    )


def _current_plan_for_week(session: Session, week_of: date_type) -> WeeklyPlan | None:
    return session.scalar(
        select(WeeklyPlan)
        .where(WeeklyPlan.week_of == week_of)
        .where(WeeklyPlan.is_current.is_(True))
        .order_by(desc(WeeklyPlan.version))
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


def _mirror_one_plan(
    plan: WeeklyPlan, client: NotionClient, database_id: str
) -> tuple[int, int, int]:
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
        session_type = _normalize_session_type(session_dict.get("session_type"))
        icon = {"type": "emoji", "emoji": SESSION_ICONS.get(session_type, SESSION_ICONS["Other"])}
        client.create_page(
            parent={"database_id": database_id},
            properties=properties,
            children=children,
            icon=icon,
        )
        created += 1
    logger.info(
        "notion.mirror.plan.week",
        week_of=plan.week_of.isoformat(),
        version=plan.version,
        sessions=len(sessions),
        archived=archived,
        created=created,
    )
    return len(sessions), archived, created


def mirror_plan(
    session: Session,
    client: NotionClient,
    database_id: str,
    *,
    week_of: date_type | None = None,
) -> dict[str, int]:
    """Push current weekly plan(s) into Notion.

    When ``week_of`` is given, only that week is mirrored — used by the MCP
    ``save_weekly_plan`` tool so saving one week doesn't churn other weeks'
    Notion pages (which would archive any in-progress logging there). When
    omitted, every is_current=True plan is mirrored — used by the scheduled
    workflow so all active weeks land in Notion.
    """
    if week_of is not None:
        plan = _current_plan_for_week(session, week_of)
        plans = [plan] if plan is not None else []
    else:
        plans = _current_plans(session)

    if not plans:
        logger.info("notion.mirror.plan.no_current_plan")
        return {"weeks": 0, "sessions": 0, "archived": 0, "created": 0}

    total_sessions = 0
    total_archived = 0
    total_created = 0
    for plan in plans:
        sessions, archived, created = _mirror_one_plan(plan, client, database_id)
        total_sessions += sessions
        total_archived += archived
        total_created += created

    logger.info(
        "notion.mirror.plan",
        weeks=len(plans),
        sessions=total_sessions,
        archived=total_archived,
        created=total_created,
    )
    return {
        "weeks": len(plans),
        "sessions": total_sessions,
        "archived": total_archived,
        "created": total_created,
    }
