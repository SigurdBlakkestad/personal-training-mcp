"""Thin wrapper around ``notion_client.Client`` adding 429 retries and logs.

Notion publishes a soft limit of ~3 requests/sec. ``HTTPResponseError`` with
status 429 is the rate-limit signal; tenacity retries it with exponential
backoff. Other 4xx errors (validation, not-found, permission) must surface
immediately so callers can handle them or fail loudly.
"""

from collections.abc import Callable
from typing import Any

from notion_client import Client
from notion_client.errors import APIResponseError, HTTPResponseError
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from training_pipeline.shared.logging import get_logger

logger = get_logger(__name__)


def _is_rate_limited(exc: BaseException) -> bool:
    if isinstance(exc, HTTPResponseError):
        return exc.status == 429
    if isinstance(exc, APIResponseError):
        return exc.status == 429
    return False


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
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max

    def _retrying(self) -> Retrying:
        return Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=1, min=self._backoff_min, max=self._backoff_max),
            retry=retry_if_exception(_is_rate_limited),
            reraise=True,
        )

    def _call(self, op_name: str, func: Callable[..., Any], **kwargs: Any) -> Any:
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

        return self._retrying()(_do)

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
        result = self._call("pages.create", self._client.pages.create, **kwargs)
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
            block_id=block_id,
            children=children,
        )
        return dict(result)

    def delete_block(self, block_id: str) -> dict[str, Any]:
        result = self._call("blocks.delete", self._client.blocks.delete, block_id=block_id)
        return dict(result)
