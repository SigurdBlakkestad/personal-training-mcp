"""Thin wrapper around ``notion_client.Client`` adding retries and logs.

Retries follow the repo policy (5xx and connection-level errors, via
``shared.retry``) plus Notion's documented rate-limit signal, 429, whose
``Retry-After`` header is honoured up to ``backoff_max``. Other 4xx errors
(validation, not-found, permission) surface immediately so callers can handle
them or fail loudly.

Writes that append content (``pages.create``, ``blocks.children.append``) are
not idempotent: a 5xx or timeout may arrive after Notion applied them, so a
retry would duplicate the page or blocks. Those ops retry only when Notion
provably did not process the request (429, or the connection never opened).

``notion_client`` wraps HTTP status errors in ``HTTPResponseError`` (or its
subclass ``APIResponseError``) and every ``httpx`` timeout in
``RequestTimeoutError``; other transport errors propagate as raw ``httpx``
exceptions.
"""

from collections.abc import Callable
from typing import Any

import httpx
from notion_client import Client
from notion_client.errors import APIResponseError, HTTPResponseError, RequestTimeoutError
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.retry import is_retryable, is_retryable_status

logger = get_logger(__name__)

_RATE_LIMITED = 429


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, HTTPResponseError):
        return exc.status == _RATE_LIMITED or is_retryable_status(exc.status)
    return isinstance(exc, RequestTimeoutError) or is_retryable(exc)


def _is_retryable_non_idempotent(exc: BaseException) -> bool:
    if isinstance(exc, HTTPResponseError):
        return exc.status == _RATE_LIMITED
    return isinstance(exc, httpx.ConnectError)


def _retry_after_seconds(exc: BaseException | None) -> float | None:
    if not isinstance(exc, HTTPResponseError) or exc.status != _RATE_LIMITED:
        return None
    raw = exc.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        max_attempts: int = 5,
        backoff_min: float = 1.0,
        backoff_max: float = 32.0,
    ) -> None:
        self._client = Client(auth=token)
        self._max_attempts = max_attempts
        self._backoff_max = backoff_max
        self._backoff = wait_exponential(multiplier=1, min=backoff_min, max=backoff_max)

    def _wait(self, retry_state: RetryCallState) -> float:
        backoff = self._backoff(retry_state)
        outcome = retry_state.outcome
        retry_after = _retry_after_seconds(outcome.exception() if outcome else None)
        if retry_after is None:
            return backoff
        return max(backoff, min(retry_after, self._backoff_max))

    def _retrying(self, *, idempotent: bool) -> Retrying:
        return Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception(_is_retryable if idempotent else _is_retryable_non_idempotent),
            reraise=True,
        )

    def _call(
        self,
        op_name: str,
        func: Callable[..., Any],
        *,
        idempotent: bool = True,
        **kwargs: Any,
    ) -> Any:
        def _do() -> Any:
            try:
                return func(**kwargs)
            except APIResponseError as exc:
                logger.warning(
                    "notion.api_error",
                    op=op_name,
                    status=exc.status,
                    code=str(exc.code),
                )
                raise
            except HTTPResponseError as exc:
                logger.warning("notion.http_error", op=op_name, status=exc.status)
                raise
            except (RequestTimeoutError, httpx.TransportError) as exc:
                logger.warning(
                    "notion.transport_error", op=op_name, error_type=exc.__class__.__name__
                )
                raise

        return self._retrying(idempotent=idempotent)(_do)

    def query_database(
        self,
        database_id: str,
        *,
        filter: dict[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"database_id": database_id, "page_size": page_size}
            if filter is not None:
                kwargs["filter"] = filter
            if cursor is not None:
                kwargs["start_cursor"] = cursor
            response = self._call("databases.query", self._client.databases.query, **kwargs)
            results.extend(response.get("results", []))
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
            if cursor is None:
                break
        logger.info("notion.query_database", database_id=database_id, result_count=len(results))
        return results

    def create_page(
        self,
        *,
        parent: dict[str, Any],
        properties: dict[str, Any],
        children: list[dict[str, Any]] | None = None,
        icon: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"parent": parent, "properties": properties}
        if children is not None:
            kwargs["children"] = children
        if icon is not None:
            kwargs["icon"] = icon
        result = self._call("pages.create", self._client.pages.create, idempotent=False, **kwargs)
        return dict(result)

    def update_page(
        self,
        page_id: str,
        *,
        properties: dict[str, Any] | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"page_id": page_id}
        if properties is not None:
            kwargs["properties"] = properties
        if archived is not None:
            kwargs["archived"] = archived
        result = self._call("pages.update", self._client.pages.update, **kwargs)
        return dict(result)

    def list_block_children(self, block_id: str, *, page_size: int = 100) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"block_id": block_id, "page_size": page_size}
            if cursor is not None:
                kwargs["start_cursor"] = cursor
            response = self._call(
                "blocks.children.list", self._client.blocks.children.list, **kwargs
            )
            results.extend(response.get("results", []))
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
            if cursor is None:
                break
        return results

    def append_block_children(
        self, block_id: str, children: list[dict[str, Any]]
    ) -> dict[str, Any]:
        result = self._call(
            "blocks.children.append",
            self._client.blocks.children.append,
            idempotent=False,
            block_id=block_id,
            children=children,
        )
        return dict(result)

    def delete_block(self, block_id: str) -> dict[str, Any]:
        result = self._call("blocks.delete", self._client.blocks.delete, block_id=block_id)
        return dict(result)
