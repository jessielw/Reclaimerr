from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from time import monotonic
from typing import Any

import niquests
from niquests.exceptions import ReadTimeout
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.utils.request import format_http_failure, should_retry_on_status
from backend.models.services.health import HealthResult

TRACEARR_API_PREFIX = "/api/v2/public"
TRACEARR_PAGE_SIZE = 100
_MIN_REQUEST_INTERVAL_SECONDS = 0.25


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _retry_after_seconds(response: object) -> float | None:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return max(0.0, min(float(str(raw)), 60.0))
    except (TypeError, ValueError):
        return None


def _is_supported_version(version: str) -> bool:
    """Accept stable Tracearr releases from 2.0.0 onward."""

    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?", version)
    return bool(match and tuple(int(part) for part in match.groups()) >= (2, 0, 0))


class TracearrClient:
    """Client for Tracearr's authenticated public API v2."""

    __slots__ = (
        "api_key",
        "base_url",
        "timeout",
        "session",
        "_request_lock",
        "_last_request_at",
    )

    def __init__(self, api_key: str, base_url: str, timeout: int = 30) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = niquests.AsyncSession()
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    @property
    def api_url(self) -> str:
        return f"{self.base_url}{TRACEARR_API_PREFIX}"

    async def _pace_request(self) -> None:
        async with self._request_lock:
            delay = _MIN_REQUEST_INTERVAL_SECONDS - (
                monotonic() - self._last_request_at
            )
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = monotonic()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        retry=(
            retry_if_exception_type((ConnectionError, TimeoutError, ReadTimeout))
            | retry_if_exception(should_retry_on_status)
        ),
    )
    async def _make_request(
        self, path: str, **params: str | int | bool
    ) -> dict[str, object]:
        await self._pace_request()
        query_params = {key: str(value) for key, value in params.items()}
        response = await self.session.get(
            f"{self.api_url}/{path.lstrip('/')}",
            params=query_params or None,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response)
            if retry_after:
                await asyncio.sleep(retry_after)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Tracearr returned a non-object response")
        return payload

    async def get_openapi_document(self) -> dict[str, object]:
        return await self._make_request("docs")

    async def get_libraries(self) -> list[Mapping[str, object]]:
        payload = await self._make_request("libraries")
        return _mapping_list(payload.get("data"))

    async def discover_servers(self) -> list[dict[str, str | None]]:
        """Return the servers exposed by Tracearr's generated v2 document."""

        document = await self.get_openapi_document()
        paths = _mapping(document.get("paths")) or {}
        server_ids: list[str] = []
        description = ""
        for path in paths.values():
            path_mapping = _mapping(path)
            if path_mapping is None:
                continue
            for operation in path_mapping.values():
                operation_mapping = _mapping(operation)
                parameters = (
                    operation_mapping.get("parameters")
                    if operation_mapping is not None
                    else None
                )
                if not isinstance(parameters, list):
                    continue
                for parameter in parameters:
                    parameter_mapping = _mapping(parameter)
                    if not parameter_mapping:
                        continue
                    if (
                        parameter_mapping.get("name") != "server_id"
                        or parameter_mapping.get("in") != "query"
                    ):
                        continue
                    schema = _mapping(parameter_mapping.get("schema"))
                    values = schema.get("enum") if schema else None
                    if isinstance(values, list):
                        server_ids = [
                            str(value)
                            for value in values
                            if isinstance(value, str) and value
                        ]
                    description = str(parameter_mapping.get("description") or "")
                    break
                if server_ids:
                    break
            if server_ids:
                break

        types_by_id: dict[str, str] = {}
        for library in await self.get_libraries():
            server_id = str(library.get("server_id") or "").strip()
            server_type = str(library.get("server_type") or "").strip().lower()
            if server_id and server_type:
                types_by_id[server_id] = server_type

        return [
            {
                "id": server_id,
                "name": self._server_name_from_description(description, server_id),
                "server_type": types_by_id.get(server_id),
            }
            for server_id in server_ids
        ]

    @staticmethod
    def _server_name_from_description(description: str, server_id: str) -> str:
        pattern = re.compile(
            rf"\*\*(?P<name>[^*]+)\*\*\s*:\s*`?{re.escape(server_id)}`?"
        )
        match = pattern.search(description)
        return match.group("name").strip() if match else server_id

    async def get_recently_added(
        self, server_id: str, *, page_size: int = 20
    ) -> list[Mapping[str, object]]:
        payload = await self._make_request(
            "recently-added",
            server_id=server_id,
            pageSize=max(1, min(page_size, TRACEARR_PAGE_SIZE)),
        )
        return _mapping_list(payload.get("data"))

    async def get_history_page(
        self,
        server_id: str,
        *,
        cursor: str | None = None,
        page_size: int = TRACEARR_PAGE_SIZE,
    ) -> tuple[list[Mapping[str, object]], str | None]:
        params: dict[str, str | int] = {
            "server_id": server_id,
            "pageSize": max(1, min(page_size, TRACEARR_PAGE_SIZE)),
        }
        if cursor:
            params["cursor"] = cursor
        payload = await self._make_request("history", **params)
        meta = _mapping(payload.get("meta"))
        next_cursor = str(meta.get("nextCursor") or "").strip() if meta else ""
        return _mapping_list(payload.get("data")), next_cursor or None

    async def get_users_page(
        self,
        *,
        cursor: str | None = None,
        page_size: int = TRACEARR_PAGE_SIZE,
    ) -> tuple[list[Mapping[str, object]], str | None]:
        params: dict[str, str | int] = {
            "pageSize": max(1, min(page_size, TRACEARR_PAGE_SIZE))
        }
        if cursor:
            params["cursor"] = cursor
        payload = await self._make_request("users", **params)
        meta = _mapping(payload.get("meta"))
        next_cursor = str(meta.get("nextCursor") or "").strip() if meta else ""
        return _mapping_list(payload.get("data")), next_cursor or None

    async def health(self) -> HealthResult:
        """Check the public API contract, reporting which part of it failed."""
        try:
            document = await self.get_openapi_document()
        except Exception as exc:
            return HealthResult.failed(
                format_http_failure(action="Tracearr health check", exception=exc)
            )

        info = _mapping(document.get("info"))
        paths = _mapping(document.get("paths"))
        version = str(info.get("version") or "") if info else ""
        required_paths = {
            f"{TRACEARR_API_PREFIX}/history",
            f"{TRACEARR_API_PREFIX}/users",
            f"{TRACEARR_API_PREFIX}/libraries",
            f"{TRACEARR_API_PREFIX}/recently-added",
        }
        if not document.get("openapi") or not info:
            return HealthResult.failed(
                "Tracearr did not return an OpenAPI document; check that the URL "
                "points at Tracearr itself and not a proxy or login page"
            )
        if info.get("title") != "Tracearr Public API":
            return HealthResult.failed(
                f"Unexpected OpenAPI title {info.get('title')!r}; expected "
                "'Tracearr Public API'"
            )
        if not _is_supported_version(version):
            return HealthResult.failed(
                f"Tracearr public API version {version or 'unknown'} is not supported; "
                "2.0.0 or newer is required"
            )
        missing = (
            sorted(required_paths - set(paths)) if paths else sorted(required_paths)
        )
        if missing:
            return HealthResult.failed(
                f"Tracearr public API is missing required path(s): {', '.join(missing)}"
            )
        return HealthResult.healthy()

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        client = TracearrClient(api_key=api_key, base_url=url, timeout=10)
        try:
            health = await client.health()
            if health:
                return True
            raise ValueError(
                health.detail
                or "Tracearr did not expose a compatible public API v2 document"
            )
        finally:
            await client.session.close()
