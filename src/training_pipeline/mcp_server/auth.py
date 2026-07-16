"""Bearer-token gate for the MCP HTTP transport.

The MCP endpoint exposes read *and* write access to the athlete's training and
health data, so it must never be reachable unauthenticated. This is a pure
ASGI middleware (not ``BaseHTTPMiddleware``) so it inspects request headers
without buffering the streamed MCP responses.

Behaviour:
- Requests outside ``protected_prefix`` (e.g. ``/health``) pass through.
- With no token configured the gate fails closed: every protected request gets
  503, so a misconfigured deploy can never regress to serving data openly.
- Otherwise the ``Authorization: Bearer <token>`` header must match the
  configured token (constant-time compare) or the request gets 401.
"""

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from training_pipeline.shared.logging import get_logger

logger = get_logger(__name__)

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class BearerAuthMiddleware:
    def __init__(self, app: object, token: str, protected_prefix: str = "/mcp") -> None:
        self.app = app
        self.token = token
        self.protected_prefix = protected_prefix

    def _is_protected(self, path: str) -> bool:
        return path == self.protected_prefix or path.startswith(self.protected_prefix + "/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_protected(scope.get("path", "")):
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        if not self.token:
            logger.error("mcp_server.auth.not_configured", path=scope.get("path"))
            await self._deny(send, 503, "mcp auth not configured")
            return

        headers = dict(scope.get("headers", []))
        provided = headers.get(b"authorization", b"").decode("latin-1")
        expected = f"Bearer {self.token}"
        if not (provided and hmac.compare_digest(provided, expected)):
            logger.warning("mcp_server.auth.denied", path=scope.get("path"))
            await self._deny(send, 401, "unauthorized")
            return

        await self.app(scope, receive, send)  # type: ignore[operator]

    async def _deny(self, send: Send, status: int, message: str) -> None:
        body = f'{{"error":"{message}"}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = ["BearerAuthMiddleware"]
