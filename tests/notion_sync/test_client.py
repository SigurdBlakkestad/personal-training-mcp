from typing import Any

import httpx
import pytest
from notion_client.errors import (
    APIErrorCode,
    APIResponseError,
    HTTPResponseError,
    RequestTimeoutError,
)
from tenacity import RetryCallState, Retrying

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


def test_retry_predicate_handles_both_error_types() -> None:
    assert client_module._is_retryable(_rate_limited_error()) is True
    assert client_module._is_retryable(_validation_error()) is False
    assert client_module._is_retryable(RuntimeError("nope")) is False


def test_retry_predicate_covers_5xx_and_connection_errors() -> None:
    request = httpx.Request("POST", "https://api.notion.com/v1/test")
    assert client_module._is_retryable(HTTPResponseError(_fake_response(502))) is True
    assert client_module._is_retryable(HTTPResponseError(_fake_response(404))) is False
    assert client_module._is_retryable(httpx.ConnectError("down", request=request)) is True
    assert client_module._is_retryable(RequestTimeoutError()) is True


_OK_BODY = {"object": "list", "results": [], "has_more": False, "next_cursor": None}


def _client_with_transport(
    responses: list[httpx.Response | Exception],
) -> tuple[NotionClient, list[httpx.Request]]:
    """Real ``notion_client`` stack over a mocked HTTP transport."""
    nc = _make_client()
    seen: list[httpx.Request] = []
    sequence = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        value = next(sequence)
        if isinstance(value, Exception):
            raise value
        return value

    nc._client.client = httpx.Client(transport=httpx.MockTransport(handler))
    return nc, seen


def test_retries_on_503_then_succeeds() -> None:
    nc, seen = _client_with_transport(
        [httpx.Response(503, text="upstream down"), httpx.Response(200, json=_OK_BODY)]
    )
    assert nc.query_database("db-1") == []
    assert len(seen) == 2


def test_retries_on_connect_error_then_succeeds() -> None:
    request = httpx.Request("POST", "https://api.notion.com/v1/databases/db-1/query")
    nc, seen = _client_with_transport(
        [httpx.ConnectError("reset", request=request), httpx.Response(200, json=_OK_BODY)]
    )
    assert nc.query_database("db-1") == []
    assert len(seen) == 2


def test_retries_on_http_429_then_succeeds() -> None:
    nc, seen = _client_with_transport(
        [
            httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"object": "error", "code": "rate_limited", "message": "slow down"},
            ),
            httpx.Response(200, json=_OK_BODY),
        ]
    )
    assert nc.query_database("db-1") == []
    assert len(seen) == 2


def test_http_400_is_not_retried() -> None:
    nc, seen = _client_with_transport(
        [
            httpx.Response(
                400,
                json={"object": "error", "code": "validation_error", "message": "bad input"},
            ),
            httpx.Response(200, json=_OK_BODY),
        ]
    )
    with pytest.raises(APIResponseError):
        nc.query_database("db-1")
    assert len(seen) == 1


def _wait_after(nc: NotionClient, exc: BaseException) -> float:
    state = RetryCallState(retry_object=Retrying(), fn=None, args=(), kwargs={})
    state.set_exception((type(exc), exc, None))
    return nc._wait(state)


def _rate_limited_with_retry_after(value: str) -> HTTPResponseError:
    response = httpx.Response(
        429,
        headers={"Retry-After": value},
        request=httpx.Request("POST", "https://api.notion.com/v1/test"),
    )
    return HTTPResponseError(response=response)


def test_wait_honours_retry_after_capped_at_backoff_max() -> None:
    nc = NotionClient("tok", backoff_min=1.0, backoff_max=32.0)
    assert _wait_after(nc, _rate_limited_with_retry_after("7")) == 7.0
    assert _wait_after(nc, _rate_limited_with_retry_after("120")) == 32.0


def test_wait_falls_back_to_backoff_without_usable_retry_after() -> None:
    nc = NotionClient("tok", backoff_min=1.0, backoff_max=32.0)
    assert _wait_after(nc, _rate_limited_with_retry_after("soon")) == 1.0
    assert _wait_after(nc, HTTPResponseError(_fake_response(503))) == 1.0


_PAGE_BODY = {"object": "page", "id": "page-1"}


def test_create_page_is_not_retried_on_5xx() -> None:
    nc, seen = _client_with_transport(
        [httpx.Response(502, text="bad gateway"), httpx.Response(200, json=_PAGE_BODY)]
    )
    with pytest.raises(HTTPResponseError):
        nc.create_page(parent={"database_id": "db"}, properties={})
    assert len(seen) == 1


def test_append_block_children_is_not_retried_on_read_timeout() -> None:
    request = httpx.Request("PATCH", "https://api.notion.com/v1/blocks/b/children")
    nc, seen = _client_with_transport(
        [httpx.ReadTimeout("slow", request=request), httpx.Response(200, json=_OK_BODY)]
    )
    with pytest.raises(RequestTimeoutError):
        nc.append_block_children("b", [])
    assert len(seen) == 1


def test_create_page_retries_when_request_never_reached_notion() -> None:
    request = httpx.Request("POST", "https://api.notion.com/v1/pages")
    nc, seen = _client_with_transport(
        [
            httpx.ConnectError("refused", request=request),
            httpx.Response(
                429, json={"object": "error", "code": "rate_limited", "message": "slow"}
            ),
            httpx.Response(200, json=_PAGE_BODY),
        ]
    )
    assert nc.create_page(parent={"database_id": "db"}, properties={}) == _PAGE_BODY
    assert len(seen) == 3
