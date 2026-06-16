from __future__ import annotations

from asyncio import Lock
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from platform import release, system
from typing import Any

import niquests
from cryptography.fernet import InvalidToken
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import MediaFavorite, MediaUserIdentity, ServiceConfig
from backend.enums import MediaType, Service
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.user_types import MEDIA_SERVERS

_PLEX_WATCHLIST_ENDPOINT = (
    "https://discover.provider.plex.tv/library/sections/watchlist/all"
)
_PLEX_METADATA_BASE_URL = "https://metadata.provider.plex.tv"
_PLEX_WATCHLIST_PAGE_SIZES = (20, 10, 5)
_PLEX_PRODUCT_NAME = "Reclaimerr"
_PLEX_CLIENT_IDENTIFIER = "reclaimerr"


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
        """Refresh favorites rows from Emby/Jellyfin and Plex watchlists."""
        async with self._refresh_lock:
            try:
                async with async_db() as session:
                    servers = (
                        all_servers
                        if all_servers is not None
                        else await self._get_configured_media_servers(session)
                    )
                    supported_emby_or_jellyfin = [
                        cfg
                        for cfg in servers
                        if cfg.service_type in {Service.EMBY, Service.JELLYFIN}
                    ]
                    supported_plex = [
                        cfg for cfg in servers if cfg.service_type is Service.PLEX
                    ]

                    if not supported_emby_or_jellyfin and not supported_plex:
                        await session.execute(sql_delete(MediaFavorite))
                        await session.commit()
                        self._last_successful_refresh = datetime.now(UTC)
                        self._last_error = None
                        return True, None

                    APPROVED_CLIENTS = (EmbyService, JellyfinService)
                    refreshed_sources = 0
                    for config in supported_emby_or_jellyfin:
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
                        refreshed_sources += 1
                        LOG.info(
                            f"Refreshed favorites snapshot for {config.service_type} "
                            f"(config_id={config.id}): {len(rows)} row(s)"
                        )

                    for config in supported_plex:
                        did_refresh = (
                            await self._refresh_plex_watchlist_snapshot_for_config(
                                session,
                                config=config,
                            )
                        )
                        if did_refresh:
                            refreshed_sources += 1

                    if refreshed_sources == 0:
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

            async with async_db() as session:
                plex_identities = (
                    await session.execute(
                        select(
                            MediaUserIdentity.username,
                            MediaUserIdentity.plex_auth_token,
                        ).where(
                            MediaUserIdentity.source_service == Service.PLEX,
                            MediaUserIdentity.user_id.is_not(None),
                        )
                    )
                ).all()

            for username, plex_auth_token in plex_identities:
                if not plex_auth_token:
                    continue
                display_username = str(username or "").strip()
                normalized = self._normalize_username(display_username)
                if not normalized:
                    continue
                entry = merged.get(normalized)
                if entry is None:
                    entry = {"username": display_username, "sources": set()}
                    merged[normalized] = entry
                entry["sources"].add(Service.PLEX.value)

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

    async def _refresh_plex_watchlist_snapshot_for_config(
        self,
        session: AsyncSession,
        *,
        config: ServiceConfig,
    ) -> bool:
        """Refresh favorites snapshot rows for a Plex config from linked users' watchlists."""
        identities = (
            (
                await session.execute(
                    select(MediaUserIdentity).where(
                        MediaUserIdentity.source_service == Service.PLEX,
                        MediaUserIdentity.source_service_config_id == config.id,
                        MediaUserIdentity.user_id.is_not(None),
                        MediaUserIdentity.plex_auth_token.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        if not identities:
            await session.execute(
                sql_delete(MediaFavorite).where(
                    MediaFavorite.source_service == Service.PLEX,
                    MediaFavorite.source_service_config_id == config.id,
                )
            )
            await session.commit()
            LOG.info(
                "Refreshed favorites snapshot for Service.PLEX "
                f"(config_id={config.id}): 0 row(s), no linked Plex users"
            )
            return True

        rows: list[MediaFavorite] = []
        failed_usernames: set[str] = set()
        successful_users = 0
        for identity in identities:
            username = (identity.username or "").strip()
            normalized = self._normalize_username(username)
            if not normalized:
                continue

            raw_token = str(identity.plex_auth_token or "").strip()
            if not raw_token:
                continue
            try:
                user_token = fer_decrypt(raw_token)
            except InvalidToken:
                user_token = raw_token
            user_token = user_token.strip()
            if not user_token:
                continue

            try:
                watchlist = await self._fetch_plex_watchlist_tmdb_ids(user_token)
            except Exception as exc:
                failed_usernames.add(normalized)
                LOG.warning(
                    "Plex watchlist favorites refresh failed for "
                    f"user={username!r} (config_id={config.id}): {exc}"
                )
                continue

            if movie_ids := watchlist.get(MediaType.MOVIE):
                rows.extend(
                    self._build_favorite_rows(
                        media_type=MediaType.MOVIE,
                        source_service=Service.PLEX,
                        source_service_config_id=config.id,
                        username_to_tmdb={username: movie_ids},
                    )
                )
            if series_ids := watchlist.get(MediaType.SERIES):
                rows.extend(
                    self._build_favorite_rows(
                        media_type=MediaType.SERIES,
                        source_service=Service.PLEX,
                        source_service_config_id=config.id,
                        username_to_tmdb={username: series_ids},
                    )
                )
            successful_users += 1

        if successful_users == 0:
            if failed_usernames:
                LOG.warning(
                    "Plex watchlist refresh failed for all eligible linked users "
                    f"(config_id={config.id}); keeping stale favorites rows"
                )
                return False
            await session.execute(
                sql_delete(MediaFavorite).where(
                    MediaFavorite.source_service == Service.PLEX,
                    MediaFavorite.source_service_config_id == config.id,
                )
            )
            await session.commit()
            LOG.info(
                "Refreshed favorites snapshot for Service.PLEX "
                f"(config_id={config.id}): 0 row(s), no usable Plex tokens"
            )
            return True

        if failed_usernames:
            stale_rows = (
                await session.execute(
                    select(
                        MediaFavorite.media_type,
                        MediaFavorite.tmdb_id,
                        MediaFavorite.username,
                        MediaFavorite.username_normalized,
                    ).where(
                        MediaFavorite.source_service == Service.PLEX,
                        MediaFavorite.source_service_config_id == config.id,
                        MediaFavorite.username_normalized.in_(failed_usernames),
                    )
                )
            ).all()
            for media_type, tmdb_id, username, username_normalized in stale_rows:
                rows.append(
                    MediaFavorite(
                        media_type=media_type,
                        tmdb_id=tmdb_id,
                        username=username,
                        username_normalized=username_normalized,
                        source_service=Service.PLEX,
                        source_service_config_id=config.id,
                    )
                )

        await session.execute(
            sql_delete(MediaFavorite).where(
                MediaFavorite.source_service == Service.PLEX,
                MediaFavorite.source_service_config_id == config.id,
            )
        )
        if rows:
            session.add_all(rows)
        await session.commit()
        LOG.info(
            "Refreshed favorites snapshot for Service.PLEX "
            f"(config_id={config.id}): {len(rows)} row(s), "
            f"{successful_users} user(s) refreshed, {len(failed_usernames)} user(s) failed"
        )
        return True

    async def _fetch_plex_watchlist_tmdb_ids(
        self,
        plex_user_token: str,
    ) -> dict[MediaType, set[int]]:
        """Fetch TMDb IDs for movies and series in a Plex user's watchlist."""
        ids_by_type: dict[MediaType, set[int]] = {
            MediaType.MOVIE: set(),
            MediaType.SERIES: set(),
        }
        await self._collect_plex_watchlist_ids_from_endpoint(
            endpoint=_PLEX_WATCHLIST_ENDPOINT,
            plex_user_token=plex_user_token,
            ids_by_type=ids_by_type,
        )
        return ids_by_type

    async def _collect_plex_watchlist_ids_from_endpoint(
        self,
        *,
        endpoint: str,
        plex_user_token: str,
        ids_by_type: dict[MediaType, set[int]],
    ) -> None:
        """Collect TMDb IDs for movies and series from a Plex watchlist endpoint, handling pagination."""
        headers = self._build_plex_discover_headers(plex_user_token)

        start = 0
        async with niquests.AsyncSession() as http:
            while True:
                payload, page_size = await self._request_plex_watchlist_page(
                    http=http,
                    endpoint=endpoint,
                    headers=headers,
                    start=start,
                )

                media_container = (
                    payload.get("MediaContainer", {})
                    if isinstance(payload, dict)
                    else {}
                )
                if not isinstance(media_container, dict):
                    break
                metadata = media_container.get("Metadata", [])
                if not isinstance(metadata, list) or not metadata:
                    break

                for item in metadata:
                    if not isinstance(item, dict):
                        continue
                    media_type = str(item.get("type") or "").strip().lower()
                    if media_type not in {"movie", "show"}:
                        continue
                    external_ids = PlexService._parse_external_ids(item)
                    tmdb_id = external_ids.tmdb if external_ids else None
                    if not tmdb_id:
                        rating_key = str(item.get("ratingKey") or "").strip()
                        if rating_key:
                            tmdb_id = await self._fetch_plex_item_tmdb_id(
                                http=http,
                                endpoint_base=_PLEX_METADATA_BASE_URL,
                                headers=headers,
                                rating_key=rating_key,
                            )
                    if not tmdb_id:
                        continue
                    if media_type == "movie":
                        ids_by_type[MediaType.MOVIE].add(int(tmdb_id))
                    else:
                        ids_by_type[MediaType.SERIES].add(int(tmdb_id))

                total_size = int(media_container.get("totalSize") or 0)
                items_seen = int(media_container.get("size") or len(metadata) or 0)
                if items_seen <= 0:
                    items_seen = page_size
                if items_seen <= 0:
                    break
                start += items_seen
                if total_size > 0 and start >= total_size:
                    break

    @staticmethod
    def _build_plex_discover_headers(plex_user_token: str) -> dict[str, str]:
        platform_name = system() or "Unknown"
        platform_version = release() or "0"
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Plex-Token": plex_user_token,
            "X-Plex-Platform": platform_name,
            "X-Plex-Platform-Version": platform_version,
            "X-Plex-Provides": "controller",
            "X-Plex-Product": _PLEX_PRODUCT_NAME,
            "X-Plex-Version": "1.0.0",
            "X-Plex-Device": platform_name,
            "X-Plex-Device-Name": _PLEX_PRODUCT_NAME,
            "X-Plex-Client-Identifier": _PLEX_CLIENT_IDENTIFIER,
            "X-Plex-Language": "en",
            "X-Plex-Sync-Version": "2",
            "X-Plex-Features": "external-media",
        }

    async def _request_plex_watchlist_page(
        self,
        *,
        http: niquests.AsyncSession,
        endpoint: str,
        headers: dict[str, str],
        start: int,
    ) -> tuple[dict[str, Any], int]:
        """Request a page of a Plex watchlist, trying multiple page sizes if necessary."""
        last_error: str | None = None
        for page_size in _PLEX_WATCHLIST_PAGE_SIZES:
            params = {
                "includeCollections": "1",
                "includeExternalMedia": "1",
            }
            request_headers = dict(headers)
            request_headers["X-Plex-Container-Start"] = str(start)
            request_headers["X-Plex-Container-Size"] = str(page_size)
            response = await http.get(
                endpoint,
                headers=request_headers,
                params=params,
                timeout=45,
            )
            if response.status_code is not None and response.status_code >= 400:
                body = (response.text or "").strip()
                if (
                    response.status_code == 400
                    and "x-plex-container-size" in body.lower()
                ):
                    last_error = f"HTTP {response.status_code}: invalid container size {page_size}"
                    continue
                raise RuntimeError(
                    f"HTTP {response.status_code} for {endpoint}: {body[:300]}"
                )
            payload = response.json() if response.content else {}
            if isinstance(payload, dict):
                return payload, page_size
            raise RuntimeError(
                f"Unexpected Plex watchlist payload type: {type(payload)}"
            )

        raise RuntimeError(
            f"Plex watchlist rejected all tested container sizes at start={start}: {last_error or 'unknown'}"
        )

    async def _fetch_plex_item_tmdb_id(
        self,
        *,
        http: niquests.AsyncSession,
        endpoint_base: str,
        headers: dict[str, str],
        rating_key: str,
    ) -> int | None:
        """Fetch TMDb ID for a Plex item by its rating key."""
        response = await http.get(
            f"{endpoint_base}/library/metadata/{rating_key}",
            headers=headers,
            timeout=30,
        )
        if response.status_code is not None and response.status_code >= 400:
            return None
        payload = response.json() if response.content else {}
        media_container = (
            payload.get("MediaContainer", {}) if isinstance(payload, dict) else {}
        )
        if not isinstance(media_container, dict):
            return None
        metadata = media_container.get("Metadata", [])
        if not isinstance(metadata, list) or not metadata:
            return None
        item = metadata[0]
        if not isinstance(item, dict):
            return None
        external_ids = PlexService._parse_external_ids(item)
        return external_ids.tmdb if external_ids else None

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
