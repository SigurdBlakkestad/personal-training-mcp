from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from training_pipeline.notion_sync import runner


class _FakeSettings:
    NOTION_TOKEN = "tok"
    NOTION_DB_ACTIVITIES_ID = "db-act"
    NOTION_DB_PLAN_ID = "db-plan"
    NOTION_DB_METRICS_ID = "page-dash"


@contextmanager
def _fake_get_session() -> Any:
    yield MagicMock()


def test_runner_invokes_each_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(runner, "get_session", _fake_get_session)
    monkeypatch.setattr(runner, "NotionClient", lambda token: MagicMock(name=f"client:{token}"))

    activities_mock = MagicMock(return_value={"processed": 3, "created": 1, "updated": 2})
    plan_mock = MagicMock(return_value={"sessions": 4, "archived": 0, "created": 4})
    metrics_mock = MagicMock(return_value={"deleted": 1, "appended": 12})
    monkeypatch.setattr(runner, "mirror_activities", activities_mock)
    monkeypatch.setattr(runner, "mirror_plan", plan_mock)
    monkeypatch.setattr(runner, "mirror_metrics", metrics_mock)

    result = runner.run_notion_mirror()

    activities_mock.assert_called_once()
    plan_mock.assert_called_once()
    metrics_mock.assert_called_once()
    assert result.activities["processed"] == 3
    assert result.plan["created"] == 4
    assert result.metrics["appended"] == 12


def test_runner_requires_notion_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class _MissingToken:
        NOTION_TOKEN = ""
        NOTION_DB_ACTIVITIES_ID = ""
        NOTION_DB_PLAN_ID = ""
        NOTION_DB_METRICS_ID = ""

    monkeypatch.setattr(runner, "get_settings", lambda: _MissingToken())
    with pytest.raises(RuntimeError, match="NOTION_TOKEN"):
        runner.run_notion_mirror()


def test_runner_skips_missing_database_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Partial:
        NOTION_TOKEN = "tok"
        NOTION_DB_ACTIVITIES_ID = "db-act"
        NOTION_DB_PLAN_ID = ""
        NOTION_DB_METRICS_ID = ""

    monkeypatch.setattr(runner, "get_settings", lambda: _Partial())
    monkeypatch.setattr(runner, "get_session", _fake_get_session)
    monkeypatch.setattr(runner, "NotionClient", lambda token: MagicMock())

    activities_mock = MagicMock(return_value={"processed": 0, "created": 0, "updated": 0})
    plan_mock = MagicMock()
    metrics_mock = MagicMock()
    monkeypatch.setattr(runner, "mirror_activities", activities_mock)
    monkeypatch.setattr(runner, "mirror_plan", plan_mock)
    monkeypatch.setattr(runner, "mirror_metrics", metrics_mock)

    runner.run_notion_mirror()
    activities_mock.assert_called_once()
    plan_mock.assert_not_called()
    metrics_mock.assert_not_called()
