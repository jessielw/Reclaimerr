from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, NotRequired, TypeAlias, TypedDict, cast

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
from backend.enums import MediaType, SeerrRequestStatus
from backend.models.services.seerr import SeerrPageInfo, SeerrRequest, SeerrUser

SeerrResponseData: TypeAlias = dict[str, Any] | list[dict[str, Any]]


class SeerrPageInfoData(TypedDict):
    page: int
    pages: int
    results: int


class SeerrRequestMediaData(TypedDict):
    id: int
    tmdbId: int


class SeerrRequestedByData(TypedDict):
    id: int


class SeerrRequestData(TypedDict):
    id: int
    status: int
    type: Literal["movie", "tv"]
    media: SeerrRequestMediaData
    createdAt: str
    requestedBy: SeerrRequestedByData
    is4k: NotRequired[bool]


class SeerrUserData(TypedDict):
    id: int
    username: NotRequired[str | None]
    displayName: NotRequired[str | None]
    email: NotRequired[str | None]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _page_info_from_response(data: Any, *, default_results: int = 0) -> SeerrPageInfo:
    if not isinstance(data, dict):
        return SeerrPageInfo(0, 0, default_results)
    page_info = data.get("pageInfo")
    if not isinstance(page_info, dict):
        return SeerrPageInfo(0, 0, default_results)
    return SeerrPageInfo(
        page=_as_int(page_info.get("page")),
        pages=_as_int(page_info.get("pages")),
        results=_as_int(page_info.get("results"), default_results),
    )


def _request_data_from_dict(data: dict[str, Any]) -> SeerrRequestData | None:
    media = data.get("media")
    requested_by = data.get("requestedBy")
    if not isinstance(media, dict) or not isinstance(requested_by, dict):
        return None

    request_id = _as_int_or_none(data.get("id"))
    status = _as_int_or_none(data.get("status"))
    media_id = _as_int_or_none(media.get("id"))
    tmdb_id = _as_int_or_none(media.get("tmdbId"))
    requested_by_id = _as_int_or_none(requested_by.get("id"))
    created_at = data.get("createdAt")
    request_type = data.get("type")
    if (
        request_id is None
        or status is None
        or media_id is None
        or tmdb_id is None
        or requested_by_id is None
        or request_type not in ("movie", "tv")
        or not isinstance(created_at, str)
    ):
        return None

    if request_type == "movie":
        request_type_literal: Literal["movie", "tv"] = "movie"
    elif request_type == "tv":
        request_type_literal = "tv"
    else:
        return None

    request: SeerrRequestData = {
        "id": request_id,
        "status": status,
        "type": request_type_literal,
        "media": {"id": media_id, "tmdbId": tmdb_id},
        "createdAt": created_at,
        "requestedBy": {"id": requested_by_id},
    }
    if "is4k" in data:
        request["is4k"] = bool(data.get("is4k"))
    return request


def _user_data_from_dict(data: dict[str, Any]) -> SeerrUserData | None:
    user_id = _as_int_or_none(data.get("id"))
    if user_id is None:
        return None

    user: SeerrUserData = {"id": user_id}
    username = _as_optional_str(data.get("username"))
    if username is not None:
        user["username"] = username
    display_name = _as_optional_str(data.get("displayName"))
    if display_name is not None:
        user["displayName"] = display_name
    email = _as_optional_str(data.get("email"))
    if email is not None:
        user["email"] = email
    return user


def _request_records_from_response(data: Any) -> list[SeerrRequestData]:
    if not isinstance(data, dict):
        return []
    results = data.get("results", [])
    if not isinstance(results, list):
        return []
    request_records: list[SeerrRequestData] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        request = _request_data_from_dict(item)
        if request is not None:
            request_records.append(request)
    return request_records


def _user_records_from_response(data: Any) -> tuple[SeerrPageInfo, list[SeerrUserData]]:
    if isinstance(data, list):
        users: list[SeerrUserData] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            user = _user_data_from_dict(item)
            if user is not None:
                users.append(user)
        return SeerrPageInfo(page=1, pages=1, results=len(users)), users

    if isinstance(data, dict):
        results = data.get("results")
        if not isinstance(results, list):
            alt_results = data.get("users")
            results = alt_results if isinstance(alt_results, list) else []
        users = []
        for item in results:
            if not isinstance(item, dict):
                continue
            user = _user_data_from_dict(item)
            if user is not None:
                users.append(user)
        return _page_info_from_response(data, default_results=len(users)), users

    return SeerrPageInfo(0, 0, 0), []


def _requests_from_media_response(data: Any) -> list[SeerrRequestData] | None:
    if not isinstance(data, dict):
        return None
    media_info = data.get("mediaInfo")
    if not isinstance(media_info, dict):
        return None
    requests = media_info.get("requests", [])
    if not isinstance(requests, list):
        return []
    request_records: list[SeerrRequestData] = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        request = _request_data_from_dict(item)
        if request is not None:
            request_records.append(request)
    return request_records


def _media_id_from_response(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    media_info = data.get("mediaInfo")
    if not isinstance(media_info, dict):
        return None
    return _as_int_or_none(media_info.get("id"))


def build_seerr_request_from_dict(data: SeerrRequestData) -> SeerrRequest:
    """Build SeerrRequest from API response dict."""
    media_type = MediaType.MOVIE if data["type"] == "movie" else MediaType.SERIES
    return SeerrRequest(
        id=data["id"],
        status=SeerrRequestStatus(data["status"]),
        media_id=data["media"]["id"],
        media_type=media_type,
        tmdb_id=data["media"]["tmdbId"],
        created_at=datetime.fromisoformat(data["createdAt"]),
        requested_by_id=data["requestedBy"]["id"],
        is_4k=data.get("is4k", False),
        raw=data,
    )


def build_seerr_user_from_dict(data: SeerrUserData) -> SeerrUser:
    """Build SeerrUser from API response dict."""
    return SeerrUser(
        id=data["id"],
        username=_as_optional_str(data.get("username")),
        display_name=_as_optional_str(data.get("displayName")),
        email=_as_optional_str(data.get("email")),
        raw=data,
    )


class SeerrClient:
    """Client for interacting with Seerr API."""

    __slots__ = ("api_key", "base_url", "session")

    def __init__(self, api_key: str, base_url: str) -> None:
        """Initialize Seerr client.

        Args:
            api_key: Seerr API key
            base_url: Seerr server URL
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

        self.session = niquests.AsyncSession()
        self.session.headers.update(
            {
                "X-Api-Key": self.api_key,
                "Content-Type": "application/json",
            }
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=(
            retry_if_exception_type((ConnectionError, TimeoutError, ReadTimeout))
            | retry_if_exception(should_retry_on_status)
        ),
    )
    async def _make_request(
        self,
        method: str,
        endpoint: str,
        *,
        error_context: str | None = None,
        **kwargs: Any,
    ) -> tuple[int, Any | None]:
        """Make HTTP request to Seerr API with automatic retry.

        Returns:
            Tuple of (status_code, response_data)
        """
        url = f"{self.base_url}/api/v1/{endpoint}"
        response = await self.session.request(method, url, **kwargs)

        # 403 means the API key lacks admin permissions (don't retry)
        if response.status_code == 403:
            raise PermissionError(
                f"Seerr returned 403 Forbidden for {method} {endpoint}. "
                "The configured API key does not have admin permissions. "
                "Please use an admin account's API key in Seerr's settings."
            )

        try:
            response.raise_for_status()
        except niquests.HTTPError as exc:
            if error_context:
                raise ValueError(
                    format_http_failure(
                        action=error_context,
                        exception=exc,
                        response=response,
                        method=method,
                        endpoint=endpoint,
                    )
                ) from exc
            raise

        status_code = response.status_code
        if not status_code:
            raise ValueError("Status code should not be None")

        if response.content:
            # return status_code, cast(SeerrResponseData, response.json())
            return status_code, response.json()
        return status_code, None

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            await self._make_request("GET", "auth/me")
            return True
        except Exception:
            return False

    async def get_user_requests(
        self, user_id: int, take: int = 20, skip: int = 0
    ) -> tuple[SeerrPageInfo, list[SeerrRequest]]:
        """Get all requests for a specific user.

        Args:
            user_id: User ID
            take: Number of results to return
            skip: Number of results to skip

        Returns:
            List of user's requests
        """
        _, data = await self._make_request(
            "GET",
            f"user/{user_id}/requests",
            params={"take": take, "skip": skip},
            timeout=60,
        )
        requests = _request_records_from_response(data)
        page_info = _page_info_from_response(data, default_results=len(requests))
        return page_info, [build_seerr_request_from_dict(req) for req in requests]

    async def delete_request(self, request_id: int) -> None:
        """Delete/un-request a media request.

        Args:
            request_id: Request ID to delete
        """
        status_code, _ = await self._make_request(
            "DELETE",
            f"request/{request_id}",
            error_context=f"Failed to delete request {request_id} via Seerr",
        )
        if status_code != 204:
            raise ValueError(
                f"Failed to delete request {request_id} (status: {status_code})"
            )

    async def get_requests(
        self,
        take: int = 20,
        skip: int = 0,
        filter: str = "all",
        requested_by: int | None = None,
    ) -> tuple[SeerrPageInfo, list[SeerrRequest]]:
        """Get all requests (requires appropriate permissions).

        Args:
            take: Number of results to return
            skip: Number of results to skip
            filter: Filter type (all, approved, available, pending, etc.)
            requested_by: Filter by user ID

        Returns:
            List of requests
        """
        params = {"take": take, "skip": skip, "filter": filter}
        if requested_by:
            params["requestedBy"] = requested_by

        _, data = await self._make_request("GET", "request", params=params, timeout=300)
        requests = _request_records_from_response(data)
        page_info = _page_info_from_response(data, default_results=len(requests))
        return page_info, [build_seerr_request_from_dict(req) for req in requests]

    async def get_all_requests(self, *, filter: str = "all") -> list[SeerrRequest]:
        """Fetch all requests via paginated /request."""
        take = 100
        skip = 0
        all_requests: list[SeerrRequest] = []
        while True:
            page_info, page_requests = await self.get_requests(
                take=take,
                skip=skip,
                filter=filter,
            )
            if not page_requests:
                break
            all_requests.extend(page_requests)
            skip += len(page_requests)
            if page_info.results and skip >= page_info.results:
                break
            if len(page_requests) < take:
                break
        return all_requests

    async def get_users(
        self,
        *,
        take: int = 20,
        skip: int = 0,
        q: str | None = None,
        sort: str | None = "displayname",
    ) -> tuple[SeerrPageInfo, list[SeerrUser]]:
        """Get users from Seerr's /user endpoint."""
        params: dict[str, Any] = {"take": take, "skip": skip}
        if q and q.strip():
            params["q"] = q.strip()
        if sort and sort.strip():
            params["sort"] = sort.strip()
        _, data = await self._make_request("GET", "user", params=params, timeout=120)
        page_info, users_payload = _user_records_from_response(data)
        return page_info, [build_seerr_user_from_dict(item) for item in users_payload]

    async def get_all_users(self) -> list[SeerrUser]:
        """Fetch all users via paginated /user."""
        take = 100
        skip = 0
        users: list[SeerrUser] = []
        while True:
            page_info, page_users = await self.get_users(take=take, skip=skip)
            if not page_users:
                break
            users.extend(page_users)
            skip += len(page_users)
            if page_info.results and skip >= page_info.results:
                break
            if len(page_users) < take:
                break
        return users

    async def get_movie_requests(self, tmdb_id: int) -> list[SeerrRequest]:
        """Get movie requests by TMDB ID.

        Args:
            tmdb_id: TMDB movie ID
        """
        _, data = await self._make_request("GET", f"movie/{tmdb_id}")
        requests = _requests_from_media_response(data)
        if requests is None:
            raise ValueError(f"Movie {tmdb_id} not found")
        return [build_seerr_request_from_dict(req) for req in requests]

    async def delete_movie_requests(self, tmdb_id: int) -> None:
        """Collect and delete movie requests by TMDB ID.

        Args:
            tmdb_id: TMDB movie ID
        """
        requests = await self.get_movie_requests(tmdb_id)
        for req in requests:
            await self.delete_request(req.id)

    async def get_tv_requests(self, tmdb_id: int) -> list[SeerrRequest]:
        """Get TV series requests by TMDB ID.

        Args:
            tmdb_id: TMDB TV series ID
        """
        _, data = await self._make_request("GET", f"tv/{tmdb_id}")
        requests = _requests_from_media_response(data)
        if requests is None:
            raise ValueError(f"TV series {tmdb_id} not found")
        return [build_seerr_request_from_dict(req) for req in requests]

    async def delete_tv_requests(self, tmdb_id: int) -> None:
        """Collect and delete TV series requests by TMDB ID.

        Args:
            tmdb_id: TMDB TV series ID
        """
        requests = await self.get_tv_requests(tmdb_id)
        for req in requests:
            await self.delete_request(req.id)

    async def get_media_id(self, tmdb_id: int, media_type: MediaType) -> int | None:
        """Get Seerr internal media ID from TMDB ID.

        Args:
            tmdb_id: TMDB ID
            media_type: Movie or Series

        Returns:
            Seerr media ID or None if not found
        """
        try:
            endpoint = "movie" if media_type is MediaType.MOVIE else "tv"
            _, data = await self._make_request("GET", f"{endpoint}/{tmdb_id}")
            return _media_id_from_response(data)
        except Exception:
            return None

    async def delete_media(self, media_id: int) -> None:
        """Delete media item from Seerr database.

        Args:
            media_id: Seerr internal media ID
        """
        status_code, _ = await self._make_request(
            "DELETE",
            f"media/{media_id}",
            error_context=f"Failed to delete media {media_id} via Seerr",
        )
        if status_code != 204:
            raise ValueError(
                f"Failed to delete media {media_id} (status: {status_code})"
            )

    async def delete_movie_media(self, tmdb_id: int) -> None:
        """Delete movie media item from Seerr database.

        Args:
            tmdb_id: TMDB movie ID
        """
        media_id = await self.get_media_id(tmdb_id, MediaType.MOVIE)
        if media_id:
            await self.delete_media(media_id)

    async def delete_tv_media(self, tmdb_id: int) -> None:
        """Delete TV series media item from Seerr database.

        Args:
            tmdb_id: TMDB TV series ID
        """
        media_id = await self.get_media_id(tmdb_id, MediaType.SERIES)
        if media_id:
            await self.delete_media(media_id)

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Seerr service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/api/v1/auth/me",
                headers={"X-Api-Key": api_key},
            )
            response.raise_for_status()
            if response.status_code == 200:
                return True
            raise ValueError(f"Unexpected status code: {response.status_code}")
