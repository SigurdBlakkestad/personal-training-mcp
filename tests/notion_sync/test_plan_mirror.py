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


class FakeSession:
    def __init__(self, plan: WeeklyPlan | None) -> None:
        self._plan = plan

    def scalar(self, stmt: Any) -> Any:
        return self._plan


def test_no_current_plan_is_a_noop() -> None:
    session = FakeSession(None)
    client = MagicMock()
    result = plan_mirror.mirror_plan(session, client, "db-plan")
    assert result == {"sessions": 0, "archived": 0, "created": 0}
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

    assert result == {"sessions": 2, "archived": 2, "created": 2}
    assert client.update_page.call_count == 2
    for call in client.update_page.call_args_list:
        assert call.kwargs == {"archived": True}
    assert client.create_page.call_count == 2

    first_props = client.create_page.call_args_list[0].kwargs["properties"]
    assert first_props["Session Type"]["select"]["name"] == "Cycling"
    assert first_props["Status"]["select"]["name"] == "Planned"
    assert first_props["Intensity"]["select"]["name"] == "Easy"
    assert first_props["Duration (min)"] == {"number": 45.0}


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


def test_session_without_exercises_has_no_table_but_has_comments() -> None:
    plan = _plan(
        date(2026, 5, 11),
        [
            {
                "date": "2026-05-12",
                "session_type": "cycling",
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
    # Comments heading + empty paragraph at the end.
    assert types[-2] == "heading_2"
    assert children[-2]["heading_2"]["rich_text"][0]["text"]["content"] == "Comments"
    assert types[-1] == "paragraph"
    assert children[-1]["paragraph"]["rich_text"] == []


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
