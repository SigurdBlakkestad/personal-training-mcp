from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from training_pipeline.notion_sync import plan_mirror
from training_pipeline.shared.models import WeeklyPlan


def _plan(week_of: date, sessions: list[dict[str, Any]]) -> WeeklyPlan:
    p = WeeklyPlan(
        week_of=week_of,
        version=1,
        plan=sessions,
        notes="",
        is_current=True,
    )
    p.id = uuid4()
    p.created_at = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    return p


class _ScalarsResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __iter__(self) -> Any:
        return iter(self._items)


class FakeSession:
    """Routes ``scalar`` and ``scalars`` to a single plan or a plan list.

    Pass one plan to behave like the legacy per-week lookup (``scalar``); pass
    a list to mock the new ``_current_plans`` iteration (``scalars``).
    """

    def __init__(self, plans: WeeklyPlan | list[WeeklyPlan] | None) -> None:
        if plans is None:
            self._plan = None
            self._plans: list[WeeklyPlan] = []
        elif isinstance(plans, list):
            self._plan = plans[0] if plans else None
            self._plans = plans
        else:
            self._plan = plans
            self._plans = [plans]

    def scalar(self, stmt: Any) -> Any:
        return self._plan

    def scalars(self, stmt: Any) -> _ScalarsResult:
        return _ScalarsResult(self._plans)


def test_no_current_plan_is_a_noop() -> None:
    session = FakeSession(None)
    client = MagicMock()
    result = plan_mirror.mirror_plan(session, client, "db-plan")
    assert result == {"weeks": 0, "sessions": 0, "archived": 0, "created": 0}
    client.query_database.assert_not_called()
    client.create_page.assert_not_called()


def test_mirror_archives_previous_and_creates_new() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [
            {
                "date": "2026-05-12",
                "session_type": "cycling",
                "description": "easy spin",
                "duration_min": 45,
                "intensity": "easy",
            },
            {
                "date": "2026-05-14",
                "session_type": "lifting",
                "description": "upper push",
                "duration_min": 60,
                "intensity": "moderate",
            },
        ],
    )
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = [
        {"id": "old-1"},
        {"id": "old-2"},
    ]

    result = plan_mirror.mirror_plan(session, client, "db-plan")

    assert result == {"weeks": 1, "sessions": 2, "archived": 2, "created": 2}
    assert client.update_page.call_count == 2
    for call in client.update_page.call_args_list:
        assert call.kwargs == {"archived": True}
    assert client.create_page.call_count == 2

    first_props = client.create_page.call_args_list[0].kwargs["properties"]
    assert first_props["Session Type"]["select"]["name"] == "Cycling"
    assert first_props["Status"]["select"]["name"] == "Planned"
    assert first_props["Intensity"]["select"]["name"] == "Easy"
    assert first_props["Duration (min)"] == {"number": 45.0}


def test_mirror_iterates_all_is_current_plans_when_week_not_specified() -> None:
    week1 = _plan(
        date(2026, 5, 11),
        [{"date": "2026-05-12", "session_type": "cycling", "duration_min": 45}],
    )
    week2 = _plan(
        date(2026, 5, 18),
        [
            {"date": "2026-05-19", "session_type": "lifting", "duration_min": 60},
            {"date": "2026-05-21", "session_type": "cycling", "duration_min": 90},
        ],
    )
    session = FakeSession([week1, week2])
    client = MagicMock()
    client.query_database.return_value = []

    result = plan_mirror.mirror_plan(session, client, "db-plan")

    assert result == {"weeks": 2, "sessions": 3, "archived": 0, "created": 3}
    # Each week's date-range filter must be queried independently.
    week_starts = [
        call.kwargs["filter"]["and"][1]["date"]["on_or_after"]
        for call in client.query_database.call_args_list
    ]
    assert week_starts == ["2026-05-11", "2026-05-18"]


def test_mirror_restricted_to_single_week_when_week_of_given() -> None:
    target = _plan(date(2026, 5, 18), [{"date": "2026-05-19", "session_type": "lifting"}])
    session = FakeSession(target)
    client = MagicMock()
    client.query_database.return_value = []

    result = plan_mirror.mirror_plan(session, client, "db-plan", week_of=date(2026, 5, 18))
    assert result == {"weeks": 1, "sessions": 1, "archived": 0, "created": 1}
    # Only the target week's range is queried.
    assert client.query_database.call_count == 1


def test_mirror_filters_by_week_date_range() -> None:
    plan = _plan(date(2026, 5, 11), [])
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = []

    plan_mirror.mirror_plan(session, client, "db-plan")
    filter_arg = client.query_database.call_args.kwargs["filter"]
    conditions = filter_arg["and"]
    starts = [c for c in conditions if c["property"] == "Date" and "on_or_after" in c["date"]]
    ends = [c for c in conditions if c["property"] == "Date" and "on_or_before" in c["date"]]
    assert starts[0]["date"]["on_or_after"] == "2026-05-11"
    assert ends[0]["date"]["on_or_before"] == "2026-05-17"


def test_unknown_session_type_falls_back_to_other() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [{"date": "2026-05-12", "session_type": "fencing", "duration_min": 30}],
    )
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = []

    plan_mirror.mirror_plan(session, client, "db-plan")
    props = client.create_page.call_args.kwargs["properties"]
    assert props["Session Type"]["select"]["name"] == "Other"


def test_missing_session_date_falls_back_to_week_of() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [{"session_type": "lifting", "duration_min": 60}],
    )
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = []

    plan_mirror.mirror_plan(session, client, "db-plan")
    props = client.create_page.call_args.kwargs["properties"]
    assert props["Date"]["date"]["start"] == "2026-05-11"


def test_non_dict_session_items_are_skipped() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [{"date": "2026-05-12", "session_type": "cycling", "duration_min": 30}, "garbage"],  # type: ignore[list-item]
    )
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = []

    result = plan_mirror.mirror_plan(session, client, "db-plan")
    assert result["created"] == 1
    assert result["weeks"] == 1


def test_session_without_exercises_has_no_table_but_has_notes_section() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [
            {
                "date": "2026-05-12",
                "session_type": "cycling",
                "title": "Z2 easy spin",
                "description": "easy spin",
                "duration_min": 45,
            }
        ],
    )
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = []

    plan_mirror.mirror_plan(session, client, "db-plan")
    children = client.create_page.call_args.kwargs["children"]
    types = [block["type"] for block in children]
    assert "table" not in types
    assert types[0] == "paragraph"
    assert children[0]["paragraph"]["rich_text"][0]["text"]["content"] == "easy spin"
    # Notes heading + empty paragraph at the end (replaces old Comments label).
    assert types[-2] == "heading_2"
    assert children[-2]["heading_2"]["rich_text"][0]["text"]["content"] == "Notes"
    assert types[-1] == "paragraph"
    assert children[-1]["paragraph"]["rich_text"] == []


def test_title_field_drives_page_title_not_description() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [
            {
                "date": "2026-05-12",
                "session_type": "strength",
                "title": "Upper Push",
                "description": "WARM-UP (8 min): cat-cow x8 | t-spine x8/s\nMAIN: bench press 4x8",
                "duration_min": 60,
            }
        ],
    )
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = []

    plan_mirror.mirror_plan(session, client, "db-plan")
    props = client.create_page.call_args.kwargs["properties"]
    assert props["Session"]["title"][0]["text"]["content"] == "Upper Push"
    # ``strength`` alias collapses to the canonical Lifting select option.
    assert props["Session Type"]["select"]["name"] == "Lifting"
    # Description is no longer mirrored into a property — only the body.
    assert "Description" not in props


def test_session_icon_is_set_from_session_type() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [
            {"date": "2026-05-12", "session_type": "cycling", "title": "Z2"},
            {"date": "2026-05-13", "session_type": "rest", "title": "OFF"},
            {"date": "2026-05-14", "session_type": "strength", "title": "Pull"},
        ],
    )
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = []

    plan_mirror.mirror_plan(session, client, "db-plan")
    icons = [c.kwargs["icon"]["emoji"] for c in client.create_page.call_args_list]
    assert icons == ["🚴", "💤", "🏋️"]


def test_description_section_headers_render_bold() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [
            {
                "date": "2026-05-12",
                "session_type": "strength",
                "title": "Upper Push",
                "description": (
                    "WARM-UP (8 min): cat-cow x8 | t-spine x8/s\n"
                    "\n"
                    "MAIN:\n"
                    "1) DB press 4x8-10 @ RIR 2\n"
                    "2) Row 4x10"
                ),
            }
        ],
    )
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = []

    plan_mirror.mirror_plan(session, client, "db-plan")
    children = client.create_page.call_args.kwargs["children"]
    paragraphs = [b for b in children if b["type"] == "paragraph"]
    # Blank line dropped; 4 real lines preserved + 1 empty Notes paragraph
    assert len(paragraphs) == 5
    # First paragraph carries the WARM-UP line and is bold (section header).
    first = paragraphs[0]["paragraph"]["rich_text"][0]
    assert first["text"]["content"].startswith("WARM-UP")
    assert first["annotations"]["bold"] is True
    # Plain workout line not bold.
    bench = paragraphs[2]["paragraph"]["rich_text"][0]
    assert bench["text"]["content"].startswith("1) DB press")
    assert "annotations" not in bench


def test_session_with_exercises_emits_table_block() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [
            {
                "date": "2026-05-14",
                "session_type": "lifting",
                "description": "lower body",
                "duration_min": 60,
                "exercises": [
                    {"name": "Squat", "sets": 5, "reps": 5, "weight_kg": 100.0},
                    {"name": "RDL", "sets": 3, "reps": "8-10", "weight_kg": 80, "notes": "slow"},
                    {"name": "Calf raise"},
                ],
            }
        ],
    )
    session = FakeSession(plan)
    client = MagicMock()
    client.query_database.return_value = []

    plan_mirror.mirror_plan(session, client, "db-plan")
    children = client.create_page.call_args.kwargs["children"]
    table_blocks = [b for b in children if b["type"] == "table"]
    assert len(table_blocks) == 1
    table = table_blocks[0]["table"]
    assert table["table_width"] == 6
    assert table["has_column_header"] is True

    rows = table["children"]
    # header + 3 exercise rows
    assert len(rows) == 4
    header_cells = [cell[0]["text"]["content"] for cell in rows[0]["table_row"]["cells"]]
    assert header_cells == ["Exercise", "Target", "Done reps", "Kg", "RPE", "Notes"]

    squat_cells = rows[1]["table_row"]["cells"]
    assert squat_cells[0][0]["text"]["content"] == "Squat"
    assert squat_cells[1][0]["text"]["content"] == "5×5 @100kg"
    # Done reps / Kg / RPE empty
    assert squat_cells[2] == []
    assert squat_cells[3] == []
    assert squat_cells[4] == []

    rdl_cells = rows[2]["table_row"]["cells"]
    assert rdl_cells[1][0]["text"]["content"] == "3×8-10 @80kg"
    assert rdl_cells[5][0]["text"]["content"] == "slow"

    bare_cells = rows[3]["table_row"]["cells"]
    assert bare_cells[0][0]["text"]["content"] == "Calf raise"
    # No sets/reps/weight: target column empty.
    assert bare_cells[1] == []
