from __future__ import annotations

from asyncio import Lock
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.database import async_db
from backend.database.models import (
    Episode,
    MediaWatchUser,
    MediaWatchUserEpisode,
    Movie,
    MovieVersion,
    NativePlaybackAggregate,
    NativePlaybackUser,
    Season,
    Series,
    ServiceConfig,
    SupplementalMediaMatch,
)
from backend.enums import MediaType, Service
from backend.models.media import MediaWatchSnapshot, NativePlaybackSnapshot
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.user_types import MEDIA_SERVERS


@dataclass(slots=True)
class _NativePlaybackAggregate:
    """Mutable native state while rebuilding one persisted aggregate row."""

    media_type: MediaType
    item_users: dict[tuple[str, str], NativePlaybackSnapshot] = field(
        default_factory=dict
    )

    def add(self, snapshot: NativePlaybackSnapshot) -> None:
        identity = (snapshot.source_item_id, snapshot.source_user_id)
        current = self.item_users.get(identity)
        if current is None:
            self.item_users[identity] = snapshot
            return

        current_at = current.last_activity_at
        candidate_at = snapshot.last_activity_at
        if snapshot.play_count > current.play_count or (
            snapshot.play_count == current.play_count
            and candidate_at is not None
            and (current_at is None or candidate_at > current_at)
        ):
            self.item_users[identity] = snapshot

    @property
    def has_activity(self) -> bool:
        return any(
            row.completed or row.play_count > 0 or row.last_activity_at is not None
            for row in self.item_users.values()
        )

    @property
    def play_count(self) -> int:
        return sum(row.play_count for row in self.item_users.values())

    @property
    def unique_user_count(self) -> int:
        return len({row.source_user_id for row in self.item_users.values()})

    @property
    def usernames_by_user_id(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for row in self.item_users.values():
            if row.source_username and row.source_username.strip():
                names.setdefault(row.source_user_id, row.source_username.strip())
        return names

    @property
    def usernames_complete(self) -> bool:
        return len(self.usernames_by_user_id) == self.unique_user_count

    @property
    def usernames(self) -> list[str]:
        names = set(self.usernames_by_user_id.values())
        return sorted(names, key=str.casefold)

    @property
    def last_activity_at(self) -> datetime | None:
        return max(
            (
                row.last_activity_at
                for row in self.item_users.values()
                if row.last_activity_at is not None
            ),
            default=None,
        )


class MediaWatchSnapshotCache:
    """Refreshes per user watch snapshot rows used by rule evaluation."""

    __slots__ = ("_refresh_lock",)

    _PLEX_SYNC_STATE_KEY = "watch_snapshot_sync"
    _PLEX_LAST_VIEWED_AT_KEY = "plex_last_viewed_at"
    _PLEX_LAST_FULL_SYNC_AT_KEY = "plex_last_full_sync_at"
    _PLEX_FORMAT_VERSION_KEY = "format_version"
    _SYNC_AVAILABLE_KEY = "available"
    _SYNC_LAST_SUCCESS_AT_KEY = "last_success_at"
    _SYNC_LAST_ATTEMPT_AT_KEY = "last_attempt_at"
    _SYNC_LAST_ERROR_KEY = "last_error"
    _PLEX_FORMAT_VERSION = 3
    _PLEX_OVERLAP_WINDOW = timedelta(days=1)
    _PLEX_FULL_REBUILD_INTERVAL = timedelta(days=7)
    _SNAPSHOT_REFRESH_INTERVAL = timedelta(minutes=15)
    _SNAPSHOT_RETRY_INTERVAL = timedelta(minutes=5)
    _NATIVE_PLAYBACK_SYNC_STATE_KEY = "native_playback_sync"
    _NATIVE_PLAYBACK_FORMAT_VERSION_KEY = "format_version"
    _NATIVE_PLAYBACK_AVAILABLE_KEY = "available"
    _NATIVE_PLAYBACK_LAST_SUCCESS_AT_KEY = "last_success_at"
    _NATIVE_PLAYBACK_LAST_ATTEMPT_AT_KEY = "last_attempt_at"
    _NATIVE_PLAYBACK_LAST_ERROR_KEY = "last_error"
    _NATIVE_PLAYBACK_FORMAT_VERSION = 2

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

    @classmethod
    def _set_native_playback_sync_state(
        cls,
        config: ServiceConfig,
        *,
        available: bool,
        error: str | None = None,
    ) -> None:
        extra_settings = (
            dict(config.extra_settings)
            if isinstance(config.extra_settings, dict)
            else {}
        )
        previous = extra_settings.get(cls._NATIVE_PLAYBACK_SYNC_STATE_KEY)
        state = dict(previous) if isinstance(previous, dict) else {}
        now = cls._to_utc_iso(datetime.now(UTC))
        state[cls._NATIVE_PLAYBACK_FORMAT_VERSION_KEY] = (
            cls._NATIVE_PLAYBACK_FORMAT_VERSION
        )
        state[cls._NATIVE_PLAYBACK_AVAILABLE_KEY] = available
        state[cls._NATIVE_PLAYBACK_LAST_ATTEMPT_AT_KEY] = now
        state[cls._NATIVE_PLAYBACK_LAST_ERROR_KEY] = error
        if available:
            state[cls._NATIVE_PLAYBACK_LAST_SUCCESS_AT_KEY] = now
        extra_settings[cls._NATIVE_PLAYBACK_SYNC_STATE_KEY] = state
        config.extra_settings = extra_settings

    @classmethod
    def _set_plex_sync_state(
        cls,
        config: ServiceConfig,
        *,
        available: bool,
        error: str | None = None,
    ) -> None:
        """Persist Plex snapshot health alongside its incremental cursor."""

        extra_settings = (
            dict(config.extra_settings)
            if isinstance(config.extra_settings, dict)
            else {}
        )
        previous = extra_settings.get(cls._PLEX_SYNC_STATE_KEY)
        state = dict(previous) if isinstance(previous, dict) else {}
        now = cls._to_utc_iso(datetime.now(UTC))
        state[cls._SYNC_AVAILABLE_KEY] = available
        state[cls._SYNC_LAST_ATTEMPT_AT_KEY] = now
        state[cls._SYNC_LAST_ERROR_KEY] = error
        if available:
            state[cls._SYNC_LAST_SUCCESS_AT_KEY] = now
        extra_settings[cls._PLEX_SYNC_STATE_KEY] = state
        config.extra_settings = extra_settings

    @classmethod
    def _last_success_for_config(cls, config: ServiceConfig) -> datetime | None:
        state = cls._sync_state_for_config(config)
        return cls._parse_utc_datetime(state.get(cls._SYNC_LAST_SUCCESS_AT_KEY))

    @classmethod
    def _sync_state_for_config(cls, config: ServiceConfig) -> dict[str, Any]:
        extra_settings = config.extra_settings
        if not isinstance(extra_settings, dict):
            return {}
        state_key = (
            cls._PLEX_SYNC_STATE_KEY
            if config.service_type is Service.PLEX
            else cls._NATIVE_PLAYBACK_SYNC_STATE_KEY
        )
        state = extra_settings.get(state_key)
        return dict(state) if isinstance(state, dict) else {}

    @classmethod
    def _config_snapshot_requires_refresh(
        cls,
        config: ServiceConfig,
        now: datetime,
    ) -> bool:
        state = cls._sync_state_for_config(config)
        expected_version = (
            cls._PLEX_FORMAT_VERSION
            if config.service_type is Service.PLEX
            else cls._NATIVE_PLAYBACK_FORMAT_VERSION
        )
        if (
            state.get(cls._PLEX_FORMAT_VERSION_KEY) == expected_version
            and state.get(cls._SYNC_AVAILABLE_KEY) is True
        ):
            last_success = cls._parse_utc_datetime(
                state.get(cls._SYNC_LAST_SUCCESS_AT_KEY)
            )
            return (
                last_success is None
                or now >= last_success + cls._SNAPSHOT_REFRESH_INTERVAL
            )

        last_attempt = cls._parse_utc_datetime(state.get(cls._SYNC_LAST_ATTEMPT_AT_KEY))
        return (
            last_attempt is None or now >= last_attempt + cls._SNAPSHOT_RETRY_INTERVAL
        )

    async def ensure_fresh_snapshot(
        self,
        *,
        force: bool = False,
        allow_stale_on_failure: bool = True,
    ) -> tuple[bool, str | None]:
        """Refresh persisted watch state when any configured source is stale.

        Preview callers may continue with an older snapshot after a transient
        provider failure. Destructive callers disable that fallback and use the
        per-source availability state to make affected rule values unknown.
        """

        async with async_db() as session:
            servers = list(await self._get_configured_media_servers(session))
        supported = [
            config
            for config in servers
            if config.service_type in {Service.PLEX, Service.JELLYFIN, Service.EMBY}
        ]
        if not supported:
            return True, None

        now = datetime.now(UTC)
        last_successes = [self._last_success_for_config(config) for config in supported]
        needs_refresh = force or any(
            self._config_snapshot_requires_refresh(config, now) for config in supported
        )
        if not needs_refresh:
            return True, None

        had_snapshot = all(last_success is not None for last_success in last_successes)
        ok, error = await self.refresh_snapshot(all_servers=supported)
        if ok:
            return True, None
        if allow_stale_on_failure and had_snapshot:
            return True, error
        return False, error

    @classmethod
    def _build_native_playback_rows(
        cls,
        *,
        source_service: Service,
        source_service_config_id: int,
        snapshots: list[NativePlaybackSnapshot],
    ) -> list[NativePlaybackUser]:
        merged: dict[tuple[str, str], NativePlaybackSnapshot] = {}
        for snapshot in snapshots:
            identity = (snapshot.source_item_id, snapshot.source_user_id)
            current = merged.get(identity)
            if current is None:
                merged[identity] = snapshot
                continue
            current_at = cls._as_utc_datetime(current.last_activity_at)
            candidate_at = cls._as_utc_datetime(snapshot.last_activity_at)
            if snapshot.play_count > current.play_count or (
                snapshot.play_count == current.play_count
                and candidate_at is not None
                and (current_at is None or candidate_at > current_at)
            ):
                merged[identity] = snapshot

        refreshed_at = datetime.now(UTC)
        return [
            NativePlaybackUser(
                source_service=source_service,
                source_service_config_id=source_service_config_id,
                source_item_id=snapshot.source_item_id,
                provider_media_type=snapshot.provider_media_type,
                source_user_id=snapshot.source_user_id,
                source_username=(
                    snapshot.source_username.strip()
                    if snapshot.source_username and snapshot.source_username.strip()
                    else None
                ),
                source_username_normalized=(
                    cls._normalize_user_key(snapshot.source_username)
                    if snapshot.source_username and snapshot.source_username.strip()
                    else None
                ),
                play_count=snapshot.play_count,
                completed=snapshot.completed,
                last_activity_at=cls._as_utc_datetime(snapshot.last_activity_at),
                refreshed_at=refreshed_at,
            )
            for snapshot in merged.values()
        ]

    @classmethod
    async def _build_requester_snapshots_from_native(
        cls,
        *,
        session: AsyncSession,
        source_service: Service,
        snapshots: list[NativePlaybackSnapshot],
    ) -> list[MediaWatchSnapshot]:
        """Derive completed requester-watch evidence from native item snapshots."""

        completed = [
            snapshot
            for snapshot in snapshots
            if snapshot.completed
            and snapshot.last_activity_at is not None
            and snapshot.source_username
            and snapshot.source_username.strip()
        ]
        if not completed:
            return []

        movie_ids = {
            snapshot.source_item_id
            for snapshot in completed
            if snapshot.provider_media_type == "movie"
        }
        movie_tmdb_by_source_id: dict[str, int] = {}
        if movie_ids:
            movie_rows = (
                await session.execute(
                    select(MovieVersion.service_item_id, Movie.tmdb_id)
                    .join(Movie, MovieVersion.movie_id == Movie.id)
                    .where(
                        MovieVersion.service == source_service,
                        MovieVersion.service_item_id.in_(movie_ids),
                        Movie.removed_at.is_(None),
                    )
                )
            ).all()
            movie_tmdb_by_source_id = {
                str(source_id): int(tmdb_id)
                for source_id, tmdb_id in movie_rows
                if source_id is not None and tmdb_id is not None
            }
            supplemental_rows = (
                await session.execute(
                    select(SupplementalMediaMatch.source_item_id, Movie.tmdb_id)
                    .join(Movie, SupplementalMediaMatch.movie_id == Movie.id)
                    .where(
                        SupplementalMediaMatch.source_service == source_service,
                        SupplementalMediaMatch.media_type == MediaType.MOVIE,
                        SupplementalMediaMatch.source_item_id.in_(movie_ids),
                        SupplementalMediaMatch.movie_id.is_not(None),
                        Movie.removed_at.is_(None),
                    )
                )
            ).all()
            for source_id, tmdb_id in supplemental_rows:
                if source_id is not None and tmdb_id is not None:
                    movie_tmdb_by_source_id.setdefault(str(source_id), int(tmdb_id))

        episode_ids = {
            snapshot.source_item_id
            for snapshot in completed
            if snapshot.provider_media_type == "episode"
        }
        episode_tmdb_by_source_id: dict[str, int] = {}
        episode_column = {
            Service.JELLYFIN: Episode.jellyfin_episode_id,
            Service.EMBY: Episode.emby_episode_id,
        }.get(source_service)
        if episode_ids and episode_column is not None:
            episode_rows = (
                await session.execute(
                    select(episode_column, Series.tmdb_id)
                    .join(Season, Episode.season_id == Season.id)
                    .join(Series, Season.series_id == Series.id)
                    .where(
                        episode_column.in_(episode_ids),
                        Series.removed_at.is_(None),
                    )
                )
            ).all()
            episode_tmdb_by_source_id = {
                str(source_id): int(tmdb_id)
                for source_id, tmdb_id in episode_rows
                if source_id is not None and tmdb_id is not None
            }

        result: list[MediaWatchSnapshot] = []
        for snapshot in completed:
            username = snapshot.source_username
            if username is None:
                continue
            tmdb_id = (
                movie_tmdb_by_source_id.get(snapshot.source_item_id)
                if snapshot.provider_media_type == "movie"
                else episode_tmdb_by_source_id.get(snapshot.source_item_id)
            )
            if tmdb_id is None or snapshot.last_activity_at is None:
                continue
            result.append(
                MediaWatchSnapshot(
                    media_type=(
                        MediaType.MOVIE
                        if snapshot.provider_media_type == "movie"
                        else MediaType.SERIES
                    ),
                    tmdb_id=tmdb_id,
                    watch_user_key=username.strip(),
                    last_watched_at=snapshot.last_activity_at,
                    play_count=snapshot.play_count,
                    source_item_id=(
                        snapshot.source_item_id
                        if snapshot.provider_media_type == "episode"
                        else None
                    ),
                )
            )
        return result

    @classmethod
    async def _rebuild_native_playback_aggregates(cls, session: AsyncSession) -> None:
        """Map native item snapshots to rule targets using exact service IDs."""

        # Production sessions disable autoflush. Make pending native rows visible
        # before rebuilding, regardless of which caller invokes this helper.
        await session.flush()

        movie_targets: dict[tuple[Service, str], set[int]] = defaultdict(set)
        for service, item_id, version_id in (
            await session.execute(
                select(
                    MovieVersion.service,
                    MovieVersion.service_item_id,
                    MovieVersion.id,
                )
                .join(Movie, MovieVersion.movie_id == Movie.id)
                .where(Movie.removed_at.is_(None))
            )
        ).all():
            movie_targets[(service, str(item_id))].add(version_id)

        supplemental_movie_rows = (
            await session.execute(
                select(
                    SupplementalMediaMatch.source_service,
                    SupplementalMediaMatch.source_item_id,
                    MovieVersion.id,
                )
                .join(Movie, SupplementalMediaMatch.movie_id == Movie.id)
                .join(MovieVersion, MovieVersion.movie_id == Movie.id)
                .where(
                    SupplementalMediaMatch.media_type == MediaType.MOVIE,
                    SupplementalMediaMatch.movie_id.is_not(None),
                    Movie.removed_at.is_(None),
                )
            )
        ).all()
        for service, item_id, version_id in supplemental_movie_rows:
            movie_targets[(service, str(item_id))].add(version_id)

        episode_targets: dict[tuple[Service, str], tuple[int, int, int]] = {}
        for (
            episode_id,
            season_id,
            series_id,
            jellyfin_id,
            emby_id,
        ) in (
            await session.execute(
                select(
                    Episode.id,
                    Season.id,
                    Series.id,
                    Episode.jellyfin_episode_id,
                    Episode.emby_episode_id,
                )
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .where(Series.removed_at.is_(None))
            )
        ).all():
            if jellyfin_id:
                episode_targets[(Service.JELLYFIN, str(jellyfin_id))] = (
                    episode_id,
                    season_id,
                    series_id,
                )
            if emby_id:
                episode_targets[(Service.EMBY, str(emby_id))] = (
                    episode_id,
                    season_id,
                    series_id,
                )

        aggregates: dict[tuple[int, str, int], _NativePlaybackAggregate] = {}
        source_services: dict[int, Service] = {}
        rows = (await session.execute(select(NativePlaybackUser))).scalars().all()
        for row in rows:
            source_services[row.source_service_config_id] = row.source_service
            target_keys: list[tuple[str, int]] = []
            if row.provider_media_type == "movie":
                target_keys.extend(
                    ("movie_version", version_id)
                    for version_id in movie_targets.get(
                        (row.source_service, row.source_item_id), set()
                    )
                )
            elif row.provider_media_type == "episode":
                target = episode_targets.get((row.source_service, row.source_item_id))
                if target is not None:
                    episode_id, season_id, series_id = target
                    target_keys.extend(
                        [
                            ("series", series_id),
                            ("season", season_id),
                            ("episode", episode_id),
                        ]
                    )
            if not target_keys:
                continue

            snapshot = NativePlaybackSnapshot(
                source_item_id=row.source_item_id,
                provider_media_type=(
                    "movie" if row.provider_media_type == "movie" else "episode"
                ),
                source_user_id=row.source_user_id,
                source_username=row.source_username,
                play_count=row.play_count,
                completed=row.completed,
                last_activity_at=cls._as_utc_datetime(row.last_activity_at),
            )
            for scope, target_id in target_keys:
                identity = (row.source_service_config_id, scope, target_id)
                aggregate = aggregates.setdefault(
                    identity,
                    _NativePlaybackAggregate(
                        media_type=(
                            MediaType.MOVIE
                            if scope == "movie_version"
                            else MediaType.SERIES
                        )
                    ),
                )
                aggregate.add(snapshot)

        await session.execute(sql_delete(NativePlaybackAggregate))
        refreshed_at = datetime.now(UTC)
        session.add_all(
            [
                NativePlaybackAggregate(
                    source_service=source_services[config_id],
                    source_service_config_id=config_id,
                    target_scope=scope,
                    target_id=target_id,
                    media_type=aggregate.media_type,
                    has_activity=aggregate.has_activity,
                    play_count=aggregate.play_count,
                    unique_user_count=aggregate.unique_user_count,
                    usernames=aggregate.usernames,
                    usernames_complete=aggregate.usernames_complete,
                    last_activity_at=aggregate.last_activity_at,
                    refreshed_at=refreshed_at,
                )
                for (config_id, scope, target_id), aggregate in aggregates.items()
            ]
        )

    @staticmethod
    def _as_utc_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @classmethod
    def _plex_sync_state_requires_full_rebuild(
        cls, sync_state: dict[str, Any], now: datetime
    ) -> bool:
        """Return whether persisted Plex watch state requires a complete rebuild."""
        last_viewed_at = cls._parse_utc_datetime(
            sync_state.get(cls._PLEX_LAST_VIEWED_AT_KEY)
        )
        last_full_sync_at = cls._parse_utc_datetime(
            sync_state.get(cls._PLEX_LAST_FULL_SYNC_AT_KEY)
        )
        return (
            sync_state.get(cls._PLEX_FORMAT_VERSION_KEY) != cls._PLEX_FORMAT_VERSION
            or last_viewed_at is None
            or last_full_sync_at is None
            or now >= last_full_sync_at + cls._PLEX_FULL_REBUILD_INTERVAL
        )

    @classmethod
    def _build_watch_rows(
        cls,
        *,
        source_service: Service,
        source_service_config_id: int,
        snapshots: list[MediaWatchSnapshot],
    ) -> list[MediaWatchUser]:
        merged: dict[tuple[MediaType, int, str], tuple[str, datetime, int | None]] = {}
        for snapshot in snapshots:
            watched_at_utc = cls._as_utc_datetime(snapshot.last_watched_at)
            if watched_at_utc is None:
                continue
            normalized = cls._normalize_user_key(snapshot.watch_user_key)
            if not normalized:
                continue
            identity = (snapshot.media_type, int(snapshot.tmdb_id), normalized)
            existing = merged.get(identity)
            if existing is None or watched_at_utc > existing[1]:
                merged[identity] = (
                    snapshot.watch_user_key.strip(),
                    watched_at_utc,
                    snapshot.play_count,
                )

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

    @classmethod
    async def _build_episode_watch_rows(
        cls,
        *,
        session: AsyncSession,
        source_service: Service,
        source_service_config_id: int,
        snapshots: list[MediaWatchSnapshot],
    ) -> tuple[list[MediaWatchUserEpisode], int]:
        detailed = [
            snapshot
            for snapshot in snapshots
            if snapshot.media_type is MediaType.SERIES and snapshot.source_item_id
        ]
        if not detailed:
            return [], 0

        id_column = {
            Service.PLEX: Episode.plex_rating_key,
            Service.JELLYFIN: Episode.jellyfin_episode_id,
            Service.EMBY: Episode.emby_episode_id,
        }.get(source_service)
        if id_column is None:
            return [], len(detailed)

        source_ids = {str(snapshot.source_item_id) for snapshot in detailed}
        result = await session.execute(
            select(
                id_column,
                Series.tmdb_id,
                Season.season_number,
                Episode.episode_number,
            )
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .where(id_column.in_(source_ids), Series.tmdb_id.is_not(None))
        )
        coordinates = {
            str(source_id): (int(tmdb_id), int(season_number), int(episode_number))
            for source_id, tmdb_id, season_number, episode_number in result.all()
            if source_id is not None and tmdb_id is not None
        }

        merged: dict[tuple[int, int, int, str], tuple[str, datetime, int | None]] = {}
        unmatched = 0
        for snapshot in detailed:
            coordinate = coordinates.get(str(snapshot.source_item_id))
            watched_at = cls._as_utc_datetime(snapshot.last_watched_at)
            normalized = cls._normalize_user_key(snapshot.watch_user_key)
            if coordinate is None:
                unmatched += 1
                continue
            if watched_at is None or not normalized:
                continue
            identity = (*coordinate, normalized)
            existing = merged.get(identity)
            if existing is None or watched_at > existing[1]:
                merged[identity] = (
                    snapshot.watch_user_key.strip(),
                    watched_at,
                    snapshot.play_count,
                )

        return (
            [
                MediaWatchUserEpisode(
                    series_tmdb_id=series_tmdb_id,
                    season_number=season_number,
                    episode_number=episode_number,
                    watch_user_key=watch_user_key,
                    watch_user_key_normalized=normalized,
                    source_service=source_service,
                    source_service_config_id=source_service_config_id,
                    last_watched_at=last_watched_at,
                    play_count=play_count,
                )
                for (
                    series_tmdb_id,
                    season_number,
                    episode_number,
                    normalized,
                ), (
                    watch_user_key,
                    last_watched_at,
                    play_count,
                ) in merged.items()
            ],
            unmatched,
        )

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
                        await session.execute(sql_delete(MediaWatchUserEpisode))
                        await session.execute(sql_delete(NativePlaybackUser))
                        await session.execute(sql_delete(NativePlaybackAggregate))
                        await session.commit()
                        return True, None

                    refreshed_servers = 0
                    refresh_errors: list[str] = []
                    for config in supported:
                        service_instance = await service_manager.return_service(
                            config.service_type
                        )
                        if not isinstance(
                            service_instance,
                            (PlexService, JellyfinService, EmbyService),
                        ):
                            error = "media-server client is not initialized"
                            if config.service_type in {Service.JELLYFIN, Service.EMBY}:
                                self._set_native_playback_sync_state(
                                    config,
                                    available=False,
                                    error=error,
                                )
                            else:
                                self._set_plex_sync_state(
                                    config,
                                    available=False,
                                    error=error,
                                )
                            session.add(config)
                            await session.commit()
                            refresh_errors.append(
                                f"{config.service_type.value}: {error}"
                            )
                            LOG.warning(
                                f"Watch snapshot skip for {config.service_type} "
                                f"(config_id={config.id}): client not initialized"
                            )
                            continue
                        try:
                            incremental_mode = False
                            max_viewed_at: datetime | None = None
                            native_playback_snapshots: list[NativePlaybackSnapshot] = []
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
                                now = datetime.now(UTC)
                                needs_full_rebuild = (
                                    self._plex_sync_state_requires_full_rebuild(
                                        sync_state, now
                                    )
                                )
                                if (
                                    sync_state.get(self._PLEX_FORMAT_VERSION_KEY)
                                    != self._PLEX_FORMAT_VERSION
                                ):
                                    LOG.info(
                                        "Rebuilding Plex watch snapshot after history "
                                        f"format upgrade (config_id={config.id})"
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
                                    sync_state[self._PLEX_FORMAT_VERSION_KEY] = (
                                        self._PLEX_FORMAT_VERSION
                                    )
                                extra_settings[self._PLEX_SYNC_STATE_KEY] = sync_state
                                config.extra_settings = extra_settings
                                session.add(config)
                            else:
                                native_playback_snapshots = await service_instance.get_native_playback_user_snapshots()
                                snapshots = (
                                    await self._build_requester_snapshots_from_native(
                                        session=session,
                                        source_service=config.service_type,
                                        snapshots=native_playback_snapshots,
                                    )
                                )
                        except Exception as exc:
                            await session.rollback()
                            if config.service_type in {Service.JELLYFIN, Service.EMBY}:
                                self._set_native_playback_sync_state(
                                    config,
                                    available=False,
                                    error=str(exc),
                                )
                            else:
                                self._set_plex_sync_state(
                                    config,
                                    available=False,
                                    error=str(exc),
                                )
                            session.add(config)
                            await session.commit()
                            refresh_errors.append(f"{config.service_type.value}: {exc}")
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
                        (
                            episode_rows,
                            unmatched_episode_count,
                        ) = await self._build_episode_watch_rows(
                            session=session,
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

                        if (
                            isinstance(service_instance, PlexService)
                            and incremental_mode
                        ):
                            if episode_rows:
                                existing_episode_rows = (
                                    (
                                        await session.execute(
                                            select(MediaWatchUserEpisode).where(
                                                MediaWatchUserEpisode.source_service
                                                == config.service_type,
                                                MediaWatchUserEpisode.source_service_config_id
                                                == config.id,
                                            )
                                        )
                                    )
                                    .scalars()
                                    .all()
                                )
                                episode_merged: dict[
                                    tuple[int, int, int, str],
                                    tuple[str, datetime | None, int | None],
                                ] = {
                                    (
                                        row.series_tmdb_id,
                                        row.season_number,
                                        row.episode_number,
                                        row.watch_user_key_normalized,
                                    ): (
                                        row.watch_user_key,
                                        self._as_utc_datetime(row.last_watched_at),
                                        row.play_count,
                                    )
                                    for row in existing_episode_rows
                                }
                                for episode_row in episode_rows:
                                    episode_key = (
                                        episode_row.series_tmdb_id,
                                        episode_row.season_number,
                                        episode_row.episode_number,
                                        episode_row.watch_user_key_normalized,
                                    )
                                    watched_at = self._as_utc_datetime(
                                        episode_row.last_watched_at
                                    )
                                    episode_current = episode_merged.get(episode_key)
                                    if watched_at is not None and (
                                        episode_current is None
                                        or episode_current[1] is None
                                        or watched_at > episode_current[1]
                                    ):
                                        episode_merged[episode_key] = (
                                            episode_row.watch_user_key,
                                            watched_at,
                                            episode_row.play_count,
                                        )
                                await session.execute(
                                    sql_delete(MediaWatchUserEpisode).where(
                                        MediaWatchUserEpisode.source_service
                                        == config.service_type,
                                        MediaWatchUserEpisode.source_service_config_id
                                        == config.id,
                                    )
                                )
                                session.add_all(
                                    [
                                        MediaWatchUserEpisode(
                                            series_tmdb_id=series_tmdb_id,
                                            season_number=season_number,
                                            episode_number=episode_number,
                                            watch_user_key=watch_user_key,
                                            watch_user_key_normalized=normalized,
                                            source_service=config.service_type,
                                            source_service_config_id=config.id,
                                            last_watched_at=watched_at,
                                            play_count=play_count,
                                        )
                                        for (
                                            series_tmdb_id,
                                            season_number,
                                            episode_number,
                                            normalized,
                                        ), (
                                            watch_user_key,
                                            watched_at,
                                            play_count,
                                        ) in episode_merged.items()
                                        if watched_at is not None
                                    ]
                                )
                        else:
                            await session.execute(
                                sql_delete(MediaWatchUserEpisode).where(
                                    MediaWatchUserEpisode.source_service
                                    == config.service_type,
                                    MediaWatchUserEpisode.source_service_config_id
                                    == config.id,
                                )
                            )
                            if episode_rows:
                                session.add_all(episode_rows)

                        if config.service_type in {Service.JELLYFIN, Service.EMBY}:
                            native_rows = self._build_native_playback_rows(
                                source_service=config.service_type,
                                source_service_config_id=config.id,
                                snapshots=native_playback_snapshots,
                            )
                            await session.execute(
                                sql_delete(NativePlaybackUser).where(
                                    NativePlaybackUser.source_service
                                    == config.service_type,
                                    NativePlaybackUser.source_service_config_id
                                    == config.id,
                                )
                            )
                            if native_rows:
                                session.add_all(native_rows)
                            await self._rebuild_native_playback_aggregates(session)
                            self._set_native_playback_sync_state(
                                config,
                                available=True,
                            )
                            session.add(config)
                        else:
                            self._set_plex_sync_state(
                                config,
                                available=True,
                            )
                            session.add(config)
                        await session.commit()
                        refreshed_servers += 1
                        LOG.info(
                            f"Refreshed watch snapshot for {config.service_type} "
                            f"(config_id={config.id}): {len(rows)} row(s)"
                            f", {len(episode_rows)} episode row(s), "
                            f"{unmatched_episode_count} unmatched episode event(s)"
                            + (
                                f", {len(native_playback_snapshots)} native playback row(s)"
                                if config.service_type
                                in {Service.JELLYFIN, Service.EMBY}
                                else ""
                            )
                        )

                    if refreshed_servers == 0:
                        return (
                            False,
                            "; ".join(refresh_errors)
                            or "Failed to refresh requester watch snapshot from supported servers",
                        )

                    if refresh_errors:
                        return False, "; ".join(refresh_errors)

                return True, None
            except Exception as exc:
                return False, str(exc)


media_watch_snapshot_cache = MediaWatchSnapshotCache()
