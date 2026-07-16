"""Unit tests for the MCP bearer-token gate."""

from collections.abc import Awaitable, Callable

import pytest

from training_pipeline.mcp_server.auth import BearerAuthMiddleware

Send = Callable[[dict], Awaitable[None]]


async def _ok_app(scope: dict, receive: object, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"reached-inner-app"})


def _scope(path: str, auth: str | None = None) -> dict:
    headers = []
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return {"type": "http", "path": path, "headers": headers}


async def _run(mw: BearerAuthMiddleware, scope: dict) -> tuple[int, bytes]:
    status = -1
    body = b""

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: dict) -> None:
        nonlocal status, body
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await mw(scope, receive, send)
    return status, body


@pytest.mark.asyncio
async def test_health_passes_through_without_token() -> None:
    mw = BearerAuthMiddleware(_ok_app, token="secret")
    status, body = await _run(mw, _scope("/health"))
    assert status == 200
    assert body == b"reached-inner-app"


@pytest.mark.asyncio
async def test_mcp_with_correct_token_reaches_app() -> None:
    mw = BearerAuthMiddleware(_ok_app, token="secret")
    status, body = await _run(mw, _scope("/mcp/", auth="Bearer secret"))
    assert status == 200
    assert body == b"reached-inner-app"


@pytest.mark.asyncio
async def test_mcp_missing_token_is_rejected() -> None:
    mw = BearerAuthMiddleware(_ok_app, token="secret")
    status, _ = await _run(mw, _scope("/mcp/"))
    assert status == 401


@pytest.mark.asyncio
async def test_mcp_wrong_token_is_rejected() -> None:
    mw = BearerAuthMiddleware(_ok_app, token="secret")
    status, _ = await _run(mw, _scope("/mcp/", auth="Bearer nope"))
    assert status == 401


@pytest.mark.asyncio
async def test_mcp_fails_closed_when_token_unset() -> None:
    mw = BearerAuthMiddleware(_ok_app, token="")
    status, _ = await _run(mw, _scope("/mcp/", auth="Bearer anything"))
    assert status == 503
