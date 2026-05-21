from __future__ import annotations

from asyncio import Lock, create_task
from asyncio import Task as AsyncTask
from datetime import UTC, datetime, timedelta

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.enums import MediaType
from backend.models.services.seerr import SeerrRequest, SeerrUser


class SeerrSnapshotCache:
    """In-memory cache for Seerr user identities and request snapshots."""

    __slots__ = (
        "_users_cache",
        "_users_expires_at",
        "_users_lock",
        "_users_ttl",
        "_users_empty_ttl",
        "_request_snapshot",
        "_request_expires_at",
        "_request_last_error",
        "_request_lock",
        "_request_ttl",
        "_request_refresh_task",
    )

    def __init__(
        self,
        *,
        users_ttl: timedelta = timedelta(minutes=5),
        users_empty_ttl: timedelta = timedelta(seconds=20),
        request_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        self._users_cache: list[SeerrUser] | None = None
        self._users_expires_at: datetime | None = None
        self._users_lock = Lock()
        self._users_ttl = users_ttl
        self._users_empty_ttl = users_empty_ttl

        self._request_snapshot: dict[tuple[MediaType, int], set[int]] | None = None
        self._request_expires_at: datetime | None = None
        self._request_last_error: str | None = None
        self._request_lock = Lock()
        self._request_ttl = request_ttl
        self._request_refresh_task: AsyncTask[None] | None = None

    @staticmethod
    def _normalize_user_display(user: SeerrUser) -> SeerrUser:
        if user.display_name or user.username:
            return user
        return SeerrUser(
            id=user.id,
            username=user.username,
            display_name=f"User {user.id}",
            email=user.email,
            raw=user.raw,
        )

    def _dedupe_and_sort_users(self, users: list[SeerrUser]) -> list[SeerrUser]:
        by_id: dict[int, SeerrUser] = {}
        for user in users:
            existing = by_id.get(user.id)
            normalized = self._normalize_user_display(user)
            if existing is None:
                by_id[user.id] = normalized
                continue
            existing_score = int(bool(existing.display_name)) + int(
                bool(existing.username)
            )
            new_score = int(bool(normalized.display_name)) + int(
                bool(normalized.username)
            )
            if new_score > existing_score:
                by_id[user.id] = normalized

        deduped = list(by_id.values())
        deduped.sort(
            key=lambda item: (
                (item.display_name or item.username or "").strip().lower(),
                item.id,
            )
        )
        return deduped

    def _request_snapshot_is_fresh(self, now: datetime) -> bool:
        return (
            self._request_snapshot is not None
            and self._request_expires_at is not None
            and now < self._request_expires_at
        )

    async def _refresh_request_snapshot(self) -> tuple[bool, str | None]:
        async with self._request_lock:
            if not service_manager.seerr:
                self._request_last_error = "Seerr service is not configured"
                return False, self._request_last_error
            try:
                requests = await service_manager.seerr.get_all_requests(filter="all")
                snapshot: dict[tuple[MediaType, int], set[int]] = {}
                for req in requests:
                    key = (req.media_type, req.tmdb_id)
                    bucket = snapshot.get(key)
                    if bucket is None:
                        bucket = set()
                        snapshot[key] = bucket
                    bucket.add(req.requested_by_id)
                now = datetime.now(UTC)
                self._request_snapshot = snapshot
                self._request_expires_at = (
                    now.replace(microsecond=0) + self._request_ttl
                )
                self._request_last_error = None
                return True, None
            except Exception as exc:
                self._request_last_error = str(exc)
                return False, self._request_last_error

    async def _background_refresh_request_snapshot(self) -> None:
        try:
            ok, error = await self._refresh_request_snapshot()
            if not ok and error:
                LOG.debug(f"Background Seerr snapshot refresh failed: {error}")
        finally:
            self._request_refresh_task = None

    def _kickoff_background_request_refresh(self) -> None:
        if (
            self._request_refresh_task is not None
            and not self._request_refresh_task.done()
        ):
            return
        self._request_refresh_task = create_task(
            self._background_refresh_request_snapshot()
        )

    async def get_request_snapshot(
        self,
        *,
        require_fresh: bool,
        allow_stale_on_failure: bool,
    ) -> tuple[dict[tuple[MediaType, int], set[int]] | None, str | None]:
        now = datetime.now(UTC)

        if not require_fresh and self._request_snapshot_is_fresh(now):
            return self._request_snapshot, None

        if require_fresh:
            ok, error = await self._refresh_request_snapshot()
            if ok:
                return self._request_snapshot, None
            if allow_stale_on_failure and self._request_snapshot is not None:
                return self._request_snapshot, error
            return None, error

        if self._request_snapshot is not None:
            self._kickoff_background_request_refresh()
            return self._request_snapshot, None

        ok, error = await self._refresh_request_snapshot()
        if ok:
            return self._request_snapshot, None
        if allow_stale_on_failure and self._request_snapshot is not None:
            return self._request_snapshot, error
        return None, error

    async def _build_user_fallback_from_requests(
        self, requests: list[SeerrRequest]
    ) -> list[SeerrUser]:
        fallback: list[SeerrUser] = []
        for req in requests:
            raw_requested_by = (
                req.raw.get("requestedBy", {}) if isinstance(req.raw, dict) else {}
            )
            username = raw_requested_by.get("username")
            display_name = raw_requested_by.get("displayName")
            email = raw_requested_by.get("email")
            fallback.append(
                SeerrUser(
                    id=req.requested_by_id,
                    username=str(username) if username else None,
                    display_name=str(display_name) if display_name else None,
                    email=str(email) if email else None,
                    raw=raw_requested_by
                    if isinstance(raw_requested_by, dict)
                    else None,
                )
            )
        return fallback

    async def get_users(self, *, force_refresh: bool = False) -> list[SeerrUser]:
        async with self._users_lock:
            now = datetime.now(UTC)
            if (
                not force_refresh
                and self._users_cache is not None
                and self._users_expires_at is not None
                and now < self._users_expires_at
            ):
                return self._users_cache

            if not service_manager.seerr:
                self._users_cache = None
                self._users_expires_at = None
                return []

            try:
                users = await service_manager.seerr.get_all_users()
            except Exception as exc:
                LOG.warning(f"Failed to fetch Seerr /user list for rule picker: {exc}")
                try:
                    requests = await service_manager.seerr.get_all_requests(
                        filter="all"
                    )
                    users = await self._build_user_fallback_from_requests(requests)
                except Exception as fallback_exc:
                    LOG.warning(
                        "Failed to build Seerr user fallback from requests: "
                        f"{fallback_exc}"
                    )
                    return []

            normalized = self._dedupe_and_sort_users(users)
            self._users_cache = normalized
            self._users_expires_at = now + (
                self._users_ttl if normalized else self._users_empty_ttl
            )
            return normalized


seerr_snapshot_cache = SeerrSnapshotCache()
