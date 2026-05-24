from __future__ import annotations

from asyncio import Lock
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.database import async_db
from backend.database.models import MediaWatchUser, ServiceConfig
from backend.enums import MediaType, Service
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.types import MEDIA_SERVERS


class MediaWatchSnapshotCache:
    """Refreshes per user watch snapshot rows used by rule evaluation."""

    __slots__ = ("_refresh_lock",)

    _PLEX_SYNC_STATE_KEY = "watch_snapshot_sync"
    _PLEX_LAST_VIEWED_AT_KEY = "plex_last_viewed_at"
    _PLEX_LAST_FULL_SYNC_AT_KEY = "plex_last_full_sync_at"
    _PLEX_OVERLAP_WINDOW = timedelta(days=1)
    _PLEX_FULL_REBUILD_INTERVAL = timedelta(days=7)

    def __init__(self) -> None:
        self._refresh_lock = Lock()

    async def _get_configured_media_servers(
        self, session: AsyncSession
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

    @staticmethod
    def _normalize_user_key(value: str) -> str:
        return value.strip().lower()

    @staticmethod
    def _parse_utc_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        try:
            if raw.endswith("Z"):
                raw = f"{raw[:-1]}+00:00"
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _to_utc_iso(value: datetime) -> str:
        normalized = MediaWatchSnapshotCache._as_utc_datetime(value)
        if normalized is None:
            normalized = datetime.now(UTC)
        return normalized.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _as_utc_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @classmethod
    def _build_watch_rows(
        cls,
        *,
        source_service: Service,
        source_service_config_id: int,
        snapshots: list[tuple[MediaType, int, str, datetime, int | None]],
    ) -> list[MediaWatchUser]:
        merged: dict[tuple[MediaType, int, str], tuple[str, datetime, int | None]] = {}
        for media_type, tmdb_id, user_key, last_watched_at, play_count in snapshots:
            watched_at_utc = cls._as_utc_datetime(last_watched_at)
            if watched_at_utc is None:
                continue
            normalized = cls._normalize_user_key(str(user_key or ""))
            if not normalized:
                continue
            identity = (media_type, int(tmdb_id), normalized)
            existing = merged.get(identity)
            if existing is None or watched_at_utc > existing[1]:
                merged[identity] = (str(user_key).strip(), watched_at_utc, play_count)

        return [
            MediaWatchUser(
                media_type=media_type,
                tmdb_id=tmdb_id,
                watch_user_key=raw_user_key,
                watch_user_key_normalized=normalized,
                source_service=source_service,
                source_service_config_id=source_service_config_id,
                last_watched_at=last_watched_at,
                play_count=play_count,
            )
            for (media_type, tmdb_id, normalized), (
                raw_user_key,
                last_watched_at,
                play_count,
            ) in merged.items()
        ]

    async def refresh_snapshot(
        self,
        *,
        all_servers: list[ServiceConfig] | None = None,
    ) -> tuple[bool, str | None]:
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
                        if cfg.service_type
                        in {Service.PLEX, Service.JELLYFIN, Service.EMBY}
                    ]
                    if not supported:
                        await session.execute(sql_delete(MediaWatchUser))
                        await session.commit()
                        return True, None

                    refreshed_servers = 0
                    for config in supported:
                        service_instance = await service_manager.return_service(
                            config.service_type
                        )
                        if not isinstance(
                            service_instance,
                            (PlexService, JellyfinService, EmbyService),
                        ):
                            LOG.warning(
                                f"Watch snapshot skip for {config.service_type} "
                                f"(config_id={config.id}): client not initialized"
                            )
                            continue
                        try:
                            incremental_mode = False
                            max_viewed_at: datetime | None = None
                            if isinstance(service_instance, PlexService):
                                extra_settings = (
                                    dict(config.extra_settings)
                                    if isinstance(config.extra_settings, dict)
                                    else {}
                                )
                                sync_state_raw = extra_settings.get(
                                    self._PLEX_SYNC_STATE_KEY
                                )
                                sync_state = (
                                    dict(sync_state_raw)
                                    if isinstance(sync_state_raw, dict)
                                    else {}
                                )
                                last_viewed_at = self._parse_utc_datetime(
                                    sync_state.get(self._PLEX_LAST_VIEWED_AT_KEY)
                                )
                                last_full_sync_at = self._parse_utc_datetime(
                                    sync_state.get(self._PLEX_LAST_FULL_SYNC_AT_KEY)
                                )
                                now = datetime.now(UTC)
                                needs_full_rebuild = (
                                    last_viewed_at is None
                                    or last_full_sync_at is None
                                    or now
                                    >= (
                                        last_full_sync_at
                                        + self._PLEX_FULL_REBUILD_INTERVAL
                                    )
                                )
                                history_cutoff = None
                                if (
                                    not needs_full_rebuild
                                    and last_viewed_at is not None
                                ):
                                    history_cutoff = (
                                        last_viewed_at - self._PLEX_OVERLAP_WINDOW
                                    )
                                    incremental_mode = True
                                (
                                    snapshots,
                                    max_viewed_at,
                                ) = await service_instance.get_watched_user_snapshots_with_cursor(
                                    viewed_at_gte=history_cutoff,
                                    use_cache=needs_full_rebuild,
                                )

                                if max_viewed_at is not None and (
                                    last_viewed_at is None
                                    or max_viewed_at > last_viewed_at
                                ):
                                    sync_state[self._PLEX_LAST_VIEWED_AT_KEY] = (
                                        self._to_utc_iso(max_viewed_at)
                                    )
                                if needs_full_rebuild:
                                    sync_state[self._PLEX_LAST_FULL_SYNC_AT_KEY] = (
                                        self._to_utc_iso(now)
                                    )
                                extra_settings[self._PLEX_SYNC_STATE_KEY] = sync_state
                                config.extra_settings = extra_settings
                                session.add(config)
                            else:
                                snapshots = (
                                    await service_instance.get_watched_user_snapshots()
                                )
                        except Exception as exc:
                            LOG.warning(
                                f"Watch snapshot fetch failed for {config.service_type} "
                                f"(config_id={config.id}): {exc}"
                            )
                            continue

                        rows = self._build_watch_rows(
                            source_service=config.service_type,
                            source_service_config_id=config.id,
                            snapshots=snapshots,
                        )
                        if (
                            isinstance(service_instance, PlexService)
                            and incremental_mode
                        ):
                            if rows:
                                existing_rows = (
                                    (
                                        await session.execute(
                                            select(MediaWatchUser).where(
                                                MediaWatchUser.source_service
                                                == config.service_type,
                                                MediaWatchUser.source_service_config_id
                                                == config.id,
                                            )
                                        )
                                    )
                                    .scalars()
                                    .all()
                                )
                                merged: dict[
                                    tuple[MediaType, int, str],
                                    tuple[str, datetime, int | None],
                                ] = {}
                                for existing in existing_rows:
                                    existing_lva = self._as_utc_datetime(
                                        existing.last_watched_at
                                    )
                                    if existing_lva is None:
                                        continue
                                    merged[
                                        (
                                            existing.media_type,
                                            int(existing.tmdb_id),
                                            existing.watch_user_key_normalized,
                                        )
                                    ] = (
                                        existing.watch_user_key,
                                        existing_lva,
                                        existing.play_count,
                                    )
                                for row in rows:
                                    row_lva = self._as_utc_datetime(row.last_watched_at)
                                    if row_lva is None:
                                        continue
                                    key = (
                                        row.media_type,
                                        int(row.tmdb_id),
                                        row.watch_user_key_normalized,
                                    )
                                    current = merged.get(key)
                                    if current is None or row_lva > current[1]:
                                        merged[key] = (
                                            row.watch_user_key,
                                            row_lva,
                                            row.play_count,
                                        )

                                merged_rows = [
                                    MediaWatchUser(
                                        media_type=media_type,
                                        tmdb_id=tmdb_id,
                                        watch_user_key=watch_user_key,
                                        watch_user_key_normalized=normalized_user_key,
                                        source_service=config.service_type,
                                        source_service_config_id=config.id,
                                        last_watched_at=last_watched_at,
                                        play_count=play_count,
                                    )
                                    for (
                                        media_type,
                                        tmdb_id,
                                        normalized_user_key,
                                    ), (
                                        watch_user_key,
                                        last_watched_at,
                                        play_count,
                                    ) in merged.items()
                                ]
                                await session.execute(
                                    sql_delete(MediaWatchUser).where(
                                        MediaWatchUser.source_service
                                        == config.service_type,
                                        MediaWatchUser.source_service_config_id
                                        == config.id,
                                    )
                                )
                                session.add_all(merged_rows)
                        else:
                            await session.execute(
                                sql_delete(MediaWatchUser).where(
                                    MediaWatchUser.source_service
                                    == config.service_type,
                                    MediaWatchUser.source_service_config_id
                                    == config.id,
                                )
                            )
                            if rows:
                                session.add_all(rows)
                        await session.commit()
                        refreshed_servers += 1
                        LOG.info(
                            f"Refreshed watch snapshot for {config.service_type} "
                            f"(config_id={config.id}): {len(rows)} row(s)"
                        )

                    if refreshed_servers == 0:
                        return (
                            False,
                            "Failed to refresh requester watch snapshot from supported servers",
                        )

                return True, None
            except Exception as exc:
                return False, str(exc)


media_watch_snapshot_cache = MediaWatchSnapshotCache()
