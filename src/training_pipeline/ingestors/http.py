import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from training_pipeline.shared.logging import get_logger

logger = get_logger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return isinstance(
        exc,
        httpx.ConnectError | httpx.ConnectTimeout | httpx.ReadTimeout | httpx.RemoteProtocolError,
    )


class HttpClient:
    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: float = 30.0,
        headers: Mapping[str, str] | None = None,
        max_attempts: int = 3,
        backoff_min: float = 1.0,
        backoff_max: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers=dict(headers) if headers else None,
            transport=transport,
        )
        self._max_attempts = max_attempts
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        def _do() -> httpx.Response:
            start = time.monotonic()
            response = self._client.request(
                method,
                url,
                params=params,
                data=data,
                json=json,
                headers=headers,
            )
            duration_ms = (time.monotonic() - start) * 1000.0
            logger.info(
                "http.request",
                method=method.upper(),
                path=response.request.url.path,
                status=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
            response.raise_for_status()
            return response

        retryer = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=1, min=self._backoff_min, max=self._backoff_max),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        return retryer(_do)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)
