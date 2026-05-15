from typing import Any

import httpx
import pytest
from notion_client.errors import APIErrorCode, APIResponseError, HTTPResponseError

from training_pipeline.notion_sync import client as client_module
from training_pipeline.notion_sync.client import NotionClient


def _fake_response(status: int) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        request=httpx.Request("POST", "https://api.notion.com/v1/test"),
    )


def _rate_limited_error() -> HTTPResponseError:
    return HTTPResponseError(response=_fake_response(429), message="slow down")


def _validation_error() -> APIResponseError:
    return APIResponseError(
        response=_fake_response(400),
        message="bad input",
        code=APIErrorCode.ValidationError,
    )


def _make_client() -> NotionClient:
    return NotionClient("tok", max_attempts=3, backoff_min=0.001, backoff_max=0.002)


def test_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    nc = _make_client()
    calls: list[dict[str, Any]] = []
    sequence = iter(
        [_rate_limited_error(), _rate_limited_error(), {"results": [], "has_more": False}]
    )

    def fake_query(**kwargs: Any) -> Any:
        calls.append(kwargs)
        value = next(sequence)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(nc._client.databases, "query", fake_query)
    result = nc.query_database("db-1")
    assert result == []
    assert len(calls) == 3


def test_does_not_retry_on_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    nc = _make_client()
    calls: list[dict[str, Any]] = []

    def fake_query(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise _validation_error()

    monkeypatch.setattr(nc._client.databases, "query", fake_query)
    with pytest.raises(APIResponseError):
        nc.query_database("db-1")
    assert len(calls) == 1


def test_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    nc = _make_client()
    calls: list[dict[str, Any]] = []

    def fake_query(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise _rate_limited_error()

    monkeypatch.setattr(nc._client.databases, "query", fake_query)
    with pytest.raises(HTTPResponseError):
        nc.query_database("db-1")
    assert len(calls) == 3


def test_query_database_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    nc = _make_client()
    page_one = {"results": [{"id": "a"}, {"id": "b"}], "has_more": True, "next_cursor": "cur-1"}
    page_two = {"results": [{"id": "c"}], "has_more": False, "next_cursor": None}
    sequence = iter([page_one, page_two])
    captured: list[dict[str, Any]] = []

    def fake_query(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return next(sequence)

    monkeypatch.setattr(nc._client.databases, "query", fake_query)
    result = nc.query_database("db-1", filter={"foo": "bar"})
    assert [r["id"] for r in result] == ["a", "b", "c"]
    assert captured[0]["filter"] == {"foo": "bar"}
    assert captured[1]["start_cursor"] == "cur-1"


def test_create_page_calls_pages_create(monkeypatch: pytest.MonkeyPatch) -> None:
    nc = _make_client()
    captured: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "page-1"}

    monkeypatch.setattr(nc._client.pages, "create", fake_create)
    result = nc.create_page(parent={"database_id": "db"}, properties={"a": 1})
    assert result == {"id": "page-1"}
    assert captured["parent"] == {"database_id": "db"}
    assert captured["properties"] == {"a": 1}


def test_update_page_passes_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    nc = _make_client()
    captured: dict[str, Any] = {}

    def fake_update(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": kwargs["page_id"], "archived": kwargs.get("archived")}

    monkeypatch.setattr(nc._client.pages, "update", fake_update)
    nc.update_page("page-1", archived=True)
    assert captured == {"page_id": "page-1", "archived": True}


def test_rate_limit_detector_handles_both_error_types() -> None:
    assert client_module._is_rate_limited(_rate_limited_error()) is True
    assert client_module._is_rate_limited(_validation_error()) is False
    assert client_module._is_rate_limited(RuntimeError("nope")) is False
