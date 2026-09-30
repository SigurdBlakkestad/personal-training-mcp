"""Retry predicate shared by every outbound HTTP client.

Per repo policy, retry only server errors (5xx) and connection-level failures;
client errors (4xx) surface immediately.
"""

import httpx


def is_retryable_status(status: int) -> bool:
    return 500 <= status < 600


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return is_retryable_status(exc.response.status_code)
    return isinstance(
        exc,
        httpx.ConnectError | httpx.ConnectTimeout | httpx.ReadTimeout | httpx.RemoteProtocolError,
    )


def is_connect_failure(exc: BaseException) -> bool:
    """Retry predicate for non-idempotent calls: only errors where the request
    provably never reached the server, so a retry cannot replay it."""
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
