from datetime import date
from typing import Any
from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql

from training_pipeline.notion_sync import metrics_mirror


def _render(stmt: Any) -> str:
    try:
        return str(
            stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
    except Exception:
        return str(stmt)


class FakeRowResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class FakeSession:
    def __init__(self, latest: dict[str, tuple[date, float]]) -> None:
        self.latest = latest

    def execute(self, stmt: Any) -> Any:
        text = _render(stmt)
        for metric, payload in self.latest.items():
            if f"'{metric}'" in text:
                return FakeRowResult(payload)
        return FakeRowResult(None)


def test_mirror_deletes_existing_and_appends_new() -> None:
    session = FakeSession(
        {
            "ctl": (date(2026, 5, 14), 60.0),
            "atl": (date(2026, 5, 14), 55.0),
            "tsb": (date(2026, 5, 14), 5.0),
            "weight_7d_avg": (date(2026, 5, 14), 82.5),
        }
    )
    client = MagicMock()
    client.list_block_children.return_value = [
        {"id": "block-1"},
        {"id": "block-2"},
    ]

    result = metrics_mirror.mirror_metrics(session, client, "page-dash")

    assert result["deleted"] == 2
    assert result["appended"] > 0
    assert client.delete_block.call_count == 2
    client.append_block_children.assert_called_once()
    children = client.append_block_children.call_args.args[1]
    text_blob = " ".join(
        rt["text"]["content"]
        for block in children
        for key in ("paragraph", "heading_2")
        if key in block
        for rt in block[key]["rich_text"]
    )
    assert "CTL" in text_blob
    assert "60.0" in text_blob


def test_mirror_handles_missing_metrics_gracefully() -> None:
    session = FakeSession({})
    client = MagicMock()
    client.list_block_children.return_value = []

    result = metrics_mirror.mirror_metrics(session, client, "page-dash")
    assert result["deleted"] == 0
    assert result["appended"] > 0
    children = client.append_block_children.call_args.args[1]
    paragraphs = [
        rt["text"]["content"]
        for block in children
        if "paragraph" in block
        for rt in block["paragraph"]["rich_text"]
    ]
    assert any("no data yet" in p for p in paragraphs)
