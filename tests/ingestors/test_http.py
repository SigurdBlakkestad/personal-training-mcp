from collections.abc import Callable

import httpx
import pytest

from training_pipeline.ingestors.http import HttpClient


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    transport = httpx.MockTransport(handler)
    return HttpClient(backoff_min=0.001, backoff_max=0.005, transport=transport)


def test_retry_on_503_then_success() -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return next(responses)

    client = _make_client(handler)
    try:
        response = client.get("https://example.com/api")
        assert response.status_code == 200
        assert call_count == 2
    finally:
        client.close()


def test_no_retry_on_401() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _make_client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.get("https://example.com/api")
        assert call_count == 1
    finally:
        client.close()


def test_gives_up_after_max_attempts_on_5xx() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503)

    client = _make_client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.get("https://example.com/api")
        assert call_count == 3
    finally:
        client.close()


def test_no_retry_on_404() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404)

    client = _make_client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.get("https://example.com/api")
        assert call_count == 1
    finally:
        client.close()
