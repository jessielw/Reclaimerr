from __future__ import annotations

from asyncio import Lock
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import MediaFavorite, ServiceConfig
from backend.enums import MediaType, Service
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.types import MEDIA_SERVERS


class MediaFavoritesSnapshotCache:
    """In memory freshness/cache coordinator for media_favorites snapshot rows."""

    __slots__ = (
        "_refresh_lock",
        "_last_successful_refresh",
        "_last_error",
        "_snapshot_ttl",
        "_empty_snapshot_ttl",
        "_users_lock",
        "_users_cache",
        "_users_expires_at",
        "_users_ttl",
        "_users_empty_ttl",
    )

    def __init__(
        self,
        *,
        snapshot_ttl: timedelta = timedelta(minutes=20),
        empty_snapshot_ttl: timedelta = timedelta(minutes=10),
        users_ttl: timedelta = timedelta(minutes=10),
        users_empty_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        self._refresh_lock = Lock()
        self._last_successful_refresh: datetime | None = None
        self._last_error: str | None = None
        self._snapshot_ttl = snapshot_ttl
        self._empty_snapshot_ttl = empty_snapshot_ttl
        self._users_lock = Lock()
        self._users_cache: dict[str, dict[str, Any]] | None = None
        self._users_expires_at: datetime | None = None
        self._users_ttl = users_ttl
        self._users_empty_ttl = users_empty_ttl

    async def _get_configured_media_servers(
        self,
        session: AsyncSession,
    ) -> Sequence[ServiceConfig]:
        result = await session.execute(
            select(ServiceConfig).where(
                ServiceConfig.service_type.in_(MEDIA_SERVERS),
                ServiceConfig.enabled.is_(True),
                ServiceConfig.base_url.isnot(None),
                ServiceConfig.api_key.isnot(None),
            )
        )
        return result.scalars().all()

    async def _snapshot_state(
        self,
        session: AsyncSession,
    ) -> tuple[int, datetime | None]:
        count_result = await session.execute(
            select(
                func.count(MediaFavorite.id),
                func.max(MediaFavorite.updated_at),
            )
        )
        count, max_updated_at = count_result.one()
        return int(count or 0), self._to_utc(max_updated_at)

    async def refresh_snapshot(
        self,
        *,
        all_servers: list[ServiceConfig] | None = None,
    ) -> tuple[bool, str | None]:
        """Refresh favorites rows from Emby/Jellyfin."""
        async with self._refresh_lock:
            try:
                async with async_db() as session:
                    servers = (
                        all_servers
                        if all_servers is not None
                        else await self._get_configured_media_servers(session)
                    )
                    supported = [
                        cfg
                        for cfg in servers
                        if cfg.service_type in {Service.EMBY, Service.JELLYFIN}
                    ]

                    if not supported:
                        await session.execute(sql_delete(MediaFavorite))
                        await session.commit()
                        self._last_successful_refresh = datetime.now(UTC)
                        self._last_error = None
                        return True, None

                    APPROVED_CLIENTS = (EmbyService, JellyfinService)
                    refreshed_servers = 0
                    for config in supported:
                        client = self._build_media_service_from_config(config)
                        if client is None or not isinstance(client, APPROVED_CLIENTS):
                            continue
                        try:
                            movie_map = await client.get_favorite_tmdb_ids_by_user(
                                "movie"
                            )
                            series_map = await client.get_favorite_tmdb_ids_by_user(
                                "series"
                            )
                        except Exception as exc:
                            LOG.warning(
                                f"Favorites snapshot refresh failed for {config.service_type} "
                                f"(config_id={config.id}): {exc}"
                            )
                            continue

                        rows = self._build_favorite_rows(
                            media_type=MediaType.MOVIE,
                            source_service=config.service_type,
                            source_service_config_id=config.id,
                            username_to_tmdb=movie_map,
                        )
                        rows.extend(
                            self._build_favorite_rows(
                                media_type=MediaType.SERIES,
                                source_service=config.service_type,
                                source_service_config_id=config.id,
                                username_to_tmdb=series_map,
                            )
                        )
                        await session.execute(
                            sql_delete(MediaFavorite).where(
                                MediaFavorite.source_service == config.service_type,
                                MediaFavorite.source_service_config_id == config.id,
                            )
                        )
                        if rows:
                            session.add_all(rows)
                        await session.commit()
                        refreshed_servers += 1
                        LOG.info(
                            f"Refreshed favorites snapshot for {config.service_type} "
                            f"(config_id={config.id}): {len(rows)} row(s)"
                        )

                    if refreshed_servers == 0:
                        self._last_error = "Failed to refresh favorites snapshot from all supported servers"
                        return False, self._last_error

                    self._last_successful_refresh = datetime.now(UTC)
                    self._last_error = None
                    return True, None
            except Exception as exc:
                self._last_error = str(exc)
                return False, self._last_error

    async def ensure_fresh_snapshot(
        self, *, force: bool = False
    ) -> tuple[bool, str | None]:
        """Ensure favorites snapshot is present and reasonably fresh.

        - If rows are stale or first load has no rows, performs a refresh.
        - If refresh fails but a snapshot already exists, continue with stale data.
        """
        now = datetime.now(UTC)
        async with async_db() as session:
            row_count, max_updated_at = await self._snapshot_state(session)
        last_success = self._last_successful_refresh or max_updated_at
        ttl = self._empty_snapshot_ttl if row_count == 0 else self._snapshot_ttl
        needs_refresh = force or (last_success is None or now >= (last_success + ttl))
        if not needs_refresh:
            return True, None

        had_existing_snapshot = row_count > 0 or max_updated_at is not None
        ok, error = await self.refresh_snapshot()
        if ok:
            return True, None
        if had_existing_snapshot:
            return True, error
        return False, error

    async def _load_live_users(
        self, *, force_refresh: bool
    ) -> dict[str, dict[str, Any]]:
        now = datetime.now(UTC)
        async with self._users_lock:
            if (
                not force_refresh
                and self._users_cache is not None
                and self._users_expires_at is not None
                and now < self._users_expires_at
            ):
                return dict(self._users_cache)

            existing_cache = dict(self._users_cache) if self._users_cache else None
            had_fetch_error = False
            merged: dict[str, dict[str, Any]] = {}

            async with async_db() as session:
                servers = await self._get_configured_media_servers(session)
                supported = [
                    cfg
                    for cfg in servers
                    if cfg.service_type in {Service.EMBY, Service.JELLYFIN}
                ]

            for config in supported:
                client = self._build_media_service_from_config(config)
                if client is None or not isinstance(
                    client, (EmbyService, JellyfinService)
                ):
                    continue
                try:
                    users = await client.get_users()
                except Exception as exc:
                    had_fetch_error = True
                    LOG.warning(
                        f"Favorites users lookup failed for {config.service_type} "
                        f"(config_id={config.id}): {exc}"
                    )
                    continue

                source_name = str(config.service_type.value)
                for user in users:
                    username = (user.name or "").strip()
                    normalized = self._normalize_username(username)
                    if not normalized:
                        continue
                    entry = merged.get(normalized)
                    if entry is None:
                        entry = {
                            "username": username,
                            "sources": set(),
                        }
                        merged[normalized] = entry
                    entry["sources"].add(source_name)

            if not merged and had_fetch_error and existing_cache is not None:
                return existing_cache

            self._users_cache = merged
            self._users_expires_at = now + (
                self._users_ttl if merged else self._users_empty_ttl
            )
            return dict(merged)

    async def get_favorites_user_lookup(
        self, *, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Return merged users annotated with favorites snapshot presence/count."""
        ok, error = await self.ensure_fresh_snapshot(force=force_refresh)
        if not ok and error:
            LOG.warning(
                f"Favorites snapshot refresh failed before user lookup: {error}"
            )
        live_users = await self._load_live_users(force_refresh=force_refresh)

        snapshot_entries: dict[str, dict[str, Any]] = {}
        async with async_db() as session:
            rows = (
                await session.execute(
                    select(
                        MediaFavorite.username_normalized,
                        MediaFavorite.username,
                        MediaFavorite.source_service,
                    )
                )
            ).all()

        for normalized, username, source_service in rows:
            key = str(normalized or "").strip().lower()
            if not key:
                continue
            entry = snapshot_entries.get(key)
            if entry is None:
                entry = {"username": str(username or key), "count": 0, "sources": set()}
                snapshot_entries[key] = entry
            entry["count"] += 1
            if source_service is not None:
                entry["sources"].add(str(source_service.value))

        merged_keys = set(live_users.keys()) | set(snapshot_entries.keys())
        output: list[dict[str, Any]] = []
        for key in merged_keys:
            live = live_users.get(key)
            snap = snapshot_entries.get(key)
            username = (
                (live or {}).get("username") or (snap or {}).get("username") or key
            )
            sources = sorted(
                set((live or {}).get("sources", set()))
                | set((snap or {}).get("sources", set()))
            )
            favorites_count = int((snap or {}).get("count", 0))
            output.append(
                {
                    "username": username,
                    "has_favorites": favorites_count > 0,
                    "favorites_count": favorites_count,
                    "source_count": len(sources),
                    "sources": sources,
                }
            )

        output.sort(key=lambda item: str(item.get("username", "")).lower())
        return output

    @staticmethod
    def _normalize_username(value: str) -> str:
        return value.strip().lower()

    @staticmethod
    def _build_media_service_from_config(
        config: ServiceConfig,
    ) -> JellyfinService | EmbyService | PlexService | None:
        if not config.base_url or not config.api_key:
            return None
        api_key = config.api_key
        try:
            api_key = fer_decrypt(config.api_key)
        except InvalidToken:
            # allow plaintext key fallbacks for testing
            LOG.debug(
                f"Using plaintext API key fallback for {config.service_type} "
                f"(config_id={config.id})"
            )
        if config.service_type is Service.JELLYFIN:
            return JellyfinService(api_key=api_key, base_url=config.base_url)
        if config.service_type is Service.EMBY:
            return EmbyService(api_key=api_key, base_url=config.base_url)
        return None

    @staticmethod
    def _build_favorite_rows(
        *,
        media_type: MediaType,
        source_service: Service,
        source_service_config_id: int,
        username_to_tmdb: dict[str, set[int]],
    ) -> list[MediaFavorite]:
        rows: list[MediaFavorite] = []
        for username, tmdb_ids in username_to_tmdb.items():
            normalized = MediaFavoritesSnapshotCache._normalize_username(username)
            if not normalized:
                continue
            for tmdb_id in tmdb_ids:
                rows.append(
                    MediaFavorite(
                        media_type=media_type,
                        tmdb_id=tmdb_id,
                        username=username.strip(),
                        username_normalized=normalized,
                        source_service=source_service,
                        source_service_config_id=source_service_config_id,
                    )
                )
        return rows

    @staticmethod
    def _to_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


media_favorites_snapshot_cache = MediaFavoritesSnapshotCache()
