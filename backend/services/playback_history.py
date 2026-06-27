from __future__ import annotations

import hashlib
from asyncio import Lock
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

from cryptography.fernet import InvalidToken
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.core.utils.request import format_http_failure, summarize_error_message
from backend.database import async_db
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    PlaybackHistoryAggregate,
    PlaybackHistoryEvent,
    Season,
    Series,
    SeriesServiceRef,
    ServiceConfig,
    SupplementalMediaMatch,
)
from backend.enums import MediaType, Service
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.tautulli import TautulliClient

PLAYBACK_HISTORY_STATE_KEY = "playback_history_sync"
PLAYBACK_MOVIE_MIN_SECONDS = 15
PLAYBACK_EPISODE_MIN_SECONDS = 7
PLAYBACK_TARGET_SCOPES = ("movie_version", "series", "season", "episode")
PLAYBACK_PREVIEW_REFRESH_INTERVAL = timedelta(minutes=5)

_playback_refresh_lock = Lock()

PlaybackTargetKey = tuple[str, int]


class _WatchTrackedRow(Protocol):
    """Protocol for rows that expose playback watch tracking fields."""

    view_count: int | None
    last_viewed_at: datetime | None


@dataclass(slots=True)
class PlaybackProviderStatus:
    """Status for one playback history provider refresh."""

    config_id: int
    provider: Service
    observed_service: Service
    available: bool
    error: str | None = None
    imported_events: int = 0


@dataclass(slots=True)
class PlaybackRefreshResult:
    """Summary of the latest playback refresh attempt."""

    statuses: list[PlaybackProviderStatus] = field(default_factory=list)

    @property
    def available_services(self) -> set[Service]:
        return {status.observed_service for status in self.statuses if status.available}

    @property
    def errors(self) -> list[str]:
        return [status.error for status in self.statuses if status.error]

    @property
    def has_configured_provider(self) -> bool:
        return bool(self.statuses)


@dataclass(slots=True)
class PlaybackRuleSnapshot:
    """Snapshot of playback values available to the rule engine."""

    values_by_target: dict[PlaybackTargetKey, dict[str, object]]
    available_targets: set[PlaybackTargetKey]
    target_counts: dict[str, int]
    errors: list[str]
    has_configured_provider: bool

    def unavailable_count(self, scopes: Iterable[str]) -> int:
        """Return the number of targets in the given scopes without data."""

        scope_set = set(scopes)
        available = sum(
            1 for scope, _target_id in self.available_targets if scope in scope_set
        )
        total = sum(self.target_counts.get(scope, 0) for scope in scope_set)
        return max(0, total - available)


@dataclass(slots=True)
class _NormalizedEvent:
    """Normalized playback event ready for database upsert."""

    source_event_key: str
    source_item_id: str
    provider_media_type: Literal["movie", "episode"]
    played_at: datetime
    duration_seconds: int
    source_user_id: str | None


@dataclass(slots=True)
class _Aggregate:
    """In-memory accumulator for playback history aggregates."""

    media_type: MediaType
    play_count: int = 0
    total_duration_seconds: int = 0
    longest_duration_seconds: int = 0
    users: set[str] = field(default_factory=set)
    first_activity_at: datetime | None = None
    last_activity_at: datetime | None = None

    def add(self, event: PlaybackHistoryEvent) -> None:
        """Merge one playback event into this aggregate."""

        self.play_count += 1
        self.total_duration_seconds += max(0, event.duration_seconds)
        self.longest_duration_seconds = max(
            self.longest_duration_seconds, max(0, event.duration_seconds)
        )
        if event.source_user_id:
            self.users.add(f"{event.source_service_config_id}:{event.source_user_id}")
        if self.first_activity_at is None or event.played_at < self.first_activity_at:
            self.first_activity_at = event.played_at
        if self.last_activity_at is None or event.played_at > self.last_activity_at:
            self.last_activity_at = event.played_at


def _parse_datetime(value: object) -> datetime | None:
    """Parse a provider datetime value into a naive UTC datetime."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _parse_int(value: object) -> int | None:
    """Parse a provider numeric value into an int."""

    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _event_hash(*parts: object) -> str:
    """Build a stable event key from the supplied parts."""

    raw = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _state_from(config: ServiceConfig) -> dict[str, object]:
    """Read the playback sync state stored on a service config."""

    settings = config.extra_settings if isinstance(config.extra_settings, dict) else {}
    value = settings.get(PLAYBACK_HISTORY_STATE_KEY)
    return dict(value) if isinstance(value, dict) else {}


def _config_api_key(config: ServiceConfig) -> str:
    """Return a decrypted API key, with plaintext fallback for test fixtures."""
    try:
        return fer_decrypt(config.api_key)
    except InvalidToken:
        if config.api_key.startswith("gAAAA"):
            raise RuntimeError(
                "Could not decrypt API key for "
                f"{config.service_type.value} config {config.id}; check ENCRYPTION_KEY"
            ) from None
        LOG.debug(
            "Using plaintext API key fallback for playback history provider "
            f"{config.service_type.value} (config_id={config.id})"
        )
        return config.api_key


def _provider_error_message(exc: BaseException) -> str:
    """Format a provider exception for logs and stored state."""

    return (
        summarize_error_message(
            format_http_failure(
                action="provider request failed",
                exception=exc,
            )
        )
        or exc.__class__.__name__
    )


def _last_success_from(config: ServiceConfig) -> datetime | None:
    """Return the last successful sync time stored on a config."""

    return _parse_datetime(_state_from(config).get("last_success_at"))


def _recent_status_from(config: ServiceConfig) -> PlaybackProviderStatus | None:
    """Return cached provider status if the last attempt is still fresh."""

    state = _state_from(config)
    last_attempt = _parse_datetime(state.get("last_attempt_at"))
    if (
        last_attempt is None
        or datetime.now(UTC).replace(tzinfo=None) - last_attempt
        >= PLAYBACK_PREVIEW_REFRESH_INTERVAL
    ):
        return None
    observed_raw = str(state.get("observed_service") or "").strip()
    try:
        observed_service = Service(observed_raw)
    except ValueError:
        observed_service = (
            Service.PLEX
            if config.service_type == Service.TAUTULLI
            else config.service_type
        )
    return PlaybackProviderStatus(
        config_id=config.id,
        provider=config.service_type,
        observed_service=observed_service,
        available=bool(state.get("available")),
        error=summarize_error_message(str(state.get("last_error") or "").strip())
        or None,
    )


def _set_state(
    config: ServiceConfig,
    *,
    provider: Service,
    observed_service: Service,
    available: bool,
    error: str | None,
    imported_events: int,
    last_event_at: datetime | None = None,
) -> None:
    """Persist the latest playback sync state on a service config."""

    now = datetime.now(UTC).replace(tzinfo=None)
    settings = (
        dict(config.extra_settings) if isinstance(config.extra_settings, dict) else {}
    )
    previous = _state_from(config)
    state: dict[str, object] = {
        "provider": provider.value,
        "observed_service": observed_service.value,
        "available": available,
        "last_attempt_at": now.isoformat(),
        "last_success_at": (
            now.isoformat() if available else previous.get("last_success_at")
        ),
        "last_event_at": (
            last_event_at.isoformat()
            if last_event_at
            else previous.get("last_event_at")
        ),
        "last_error": error,
        "imported_events": imported_events,
    }
    settings[PLAYBACK_HISTORY_STATE_KEY] = state
    config.extra_settings = settings


def _normalize_reporting_events(
    rows: Iterable[Mapping[str, object]],
) -> list[_NormalizedEvent]:
    """Convert reporting plugin rows into normalized playback events."""

    events: list[_NormalizedEvent] = []
    for row in rows:
        media_type_raw = str(row.get("ItemType") or "").lower()
        if media_type_raw not in {"movie", "episode"}:
            continue
        media_type: Literal["movie", "episode"] = (
            "movie" if media_type_raw == "movie" else "episode"
        )
        duration = _parse_int(row.get("PlayDuration"))
        threshold = (
            PLAYBACK_MOVIE_MIN_SECONDS
            if media_type == "movie"
            else PLAYBACK_EPISODE_MIN_SECONDS
        )
        played_at = _parse_datetime(row.get("DateCreatedUtc") or row.get("DateCreated"))
        item_id = str(row.get("ItemId") or "").strip()
        user_id = str(row.get("UserId") or "").strip() or None
        if duration is None or duration < threshold or played_at is None or not item_id:
            continue
        events.append(
            _NormalizedEvent(
                source_event_key=_event_hash(
                    row.get("DateCreated"),
                    user_id,
                    item_id,
                    media_type,
                    duration,
                ),
                source_item_id=item_id,
                provider_media_type=media_type,
                played_at=played_at,
                duration_seconds=duration,
                source_user_id=user_id,
            )
        )
    return events


def _normalize_tautulli_events(
    rows: Iterable[Mapping[str, object]],
) -> list[_NormalizedEvent]:
    """Convert Tautulli rows into normalized playback events."""

    events: list[_NormalizedEvent] = []
    for row in rows:
        media_type_raw = str(row.get("media_type") or "").lower()
        if media_type_raw not in {"movie", "episode"}:
            continue
        media_type: Literal["movie", "episode"] = (
            "movie" if media_type_raw == "movie" else "episode"
        )
        duration = _parse_int(row.get("play_duration"))
        threshold = (
            PLAYBACK_MOVIE_MIN_SECONDS
            if media_type == "movie"
            else PLAYBACK_EPISODE_MIN_SECONDS
        )
        played_at = _parse_datetime(row.get("stopped") or row.get("started"))
        item_id = str(row.get("rating_key") or "").strip()
        user_id = str(row.get("user_id") or row.get("user") or "").strip() or None
        if duration is None or duration < threshold or played_at is None or not item_id:
            continue
        event_key = str(row.get("row_id") or "").strip() or _event_hash(
            row.get("started"),
            row.get("stopped"),
            user_id,
            item_id,
            media_type,
            duration,
        )
        events.append(
            _NormalizedEvent(
                source_event_key=event_key,
                source_item_id=item_id,
                provider_media_type=media_type,
                played_at=played_at,
                duration_seconds=duration,
                source_user_id=user_id,
            )
        )
    return events


async def _identity_maps(
    session: AsyncSession, service: Service
) -> tuple[
    dict[str, tuple[int, int]],
    dict[str, tuple[int, int, int, int, int, int]],
]:
    """Load local media ID maps for imported playback events."""

    movie_map: dict[str, tuple[int, int]] = {
        str(item_id): (movie_id, tmdb_id)
        for item_id, movie_id, tmdb_id in (
            await session.execute(
                select(
                    MovieVersion.service_item_id,
                    Movie.id,
                    Movie.tmdb_id,
                )
                .join(Movie, MovieVersion.movie_id == Movie.id)
                .where(MovieVersion.service == service)
            )
        ).all()
    }
    mapped_movies = (
        await session.execute(
            select(
                SupplementalMediaMatch.source_item_id,
                Movie.id,
                Movie.tmdb_id,
            )
            .join(Movie, SupplementalMediaMatch.movie_id == Movie.id)
            .where(
                SupplementalMediaMatch.source_service == service,
                SupplementalMediaMatch.media_type == MediaType.MOVIE,
                SupplementalMediaMatch.movie_id.is_not(None),
            )
        )
    ).all()
    for item_id, movie_id, tmdb_id in mapped_movies:
        movie_map.setdefault(str(item_id), (movie_id, tmdb_id))

    episode_column = {
        Service.PLEX: Episode.plex_rating_key,
        Service.JELLYFIN: Episode.jellyfin_episode_id,
        Service.EMBY: Episode.emby_episode_id,
    }.get(service)
    episode_map: dict[str, tuple[int, int, int, int, int, int]] = {}
    if episode_column is not None:
        rows = (
            await session.execute(
                select(
                    episode_column,
                    Episode.id,
                    Season.id,
                    Series.id,
                    Series.tmdb_id,
                    Season.season_number,
                    Episode.episode_number,
                )
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .where(episode_column.is_not(None))
            )
        ).all()
        episode_map = {
            str(item_id): (
                episode_id,
                season_id,
                series_id,
                tmdb_id,
                season_number,
                episode_number,
            )
            for (
                item_id,
                episode_id,
                season_id,
                series_id,
                tmdb_id,
                season_number,
                episode_number,
            ) in rows
        }
    return movie_map, episode_map


async def _upsert_events(
    session: AsyncSession,
    *,
    config: ServiceConfig,
    provider: Service,
    observed_service: Service,
    events: list[_NormalizedEvent],
) -> int:
    """Insert new playback events and update existing ones."""

    movie_map, episode_map = await _identity_maps(session, observed_service)
    existing_by_key: dict[str, PlaybackHistoryEvent] = {}
    keys = [event.source_event_key for event in events]
    for offset in range(0, len(keys), 500):
        chunk = keys[offset : offset + 500]
        rows = (
            await session.execute(
                select(PlaybackHistoryEvent).where(
                    PlaybackHistoryEvent.source_service_config_id == config.id,
                    PlaybackHistoryEvent.source_event_key.in_(chunk),
                )
            )
        ).scalars()
        existing_by_key.update({row.source_event_key: row for row in rows})

    inserted = 0
    for event in events:
        movie_id = series_id = season_id = episode_id = tmdb_id = None
        season_number = episode_number = None
        if event.provider_media_type == "movie":
            movie_identity = movie_map.get(event.source_item_id)
            if movie_identity:
                movie_id, tmdb_id = movie_identity
        else:
            episode_identity = episode_map.get(event.source_item_id)
            if episode_identity:
                (
                    episode_id,
                    season_id,
                    series_id,
                    tmdb_id,
                    season_number,
                    episode_number,
                ) = episode_identity

        existing = existing_by_key.get(event.source_event_key)
        if existing is None:
            session.add(
                PlaybackHistoryEvent(
                    source_service=provider,
                    source_service_config_id=config.id,
                    source_event_key=event.source_event_key,
                    source_item_id=event.source_item_id,
                    provider_media_type=event.provider_media_type,
                    played_at=event.played_at,
                    duration_seconds=event.duration_seconds,
                    source_user_id=event.source_user_id,
                    tmdb_id=tmdb_id,
                    season_number=season_number,
                    episode_number=episode_number,
                    movie_id=movie_id,
                    series_id=series_id,
                    season_id=season_id,
                    episode_id=episode_id,
                )
            )
            inserted += 1
            continue

        existing.source_item_id = event.source_item_id
        existing.provider_media_type = event.provider_media_type
        existing.played_at = event.played_at
        existing.duration_seconds = event.duration_seconds
        existing.source_user_id = event.source_user_id
        if tmdb_id is not None:
            existing.tmdb_id = tmdb_id
            existing.season_number = season_number
            existing.episode_number = episode_number
            existing.movie_id = movie_id
            existing.series_id = series_id
            existing.season_id = season_id
            existing.episode_id = episode_id
    return inserted


async def _persist_unavailable_status(
    config: ServiceConfig,
    *,
    provider: Service,
    observed_service: Service,
    error: str | None,
) -> PlaybackProviderStatus:
    """Persist a provider outcome without swallowing database failures."""

    async with async_db() as session:
        db_config = await session.get(ServiceConfig, config.id)
        if db_config:
            _set_state(
                db_config,
                provider=provider,
                observed_service=observed_service,
                available=False,
                error=error,
                imported_events=0,
            )
            await session.commit()
    return PlaybackProviderStatus(
        config_id=config.id,
        provider=provider,
        observed_service=observed_service,
        available=False,
        error=error,
    )


async def _refresh_reporting_config(
    config: ServiceConfig,
) -> PlaybackProviderStatus:
    """Refresh one Jellyfin or Emby playback source."""

    service_cls = (
        JellyfinService if config.service_type == Service.JELLYFIN else EmbyService
    )
    client = None
    try:
        try:
            client = service_cls(
                api_key=_config_api_key(config), base_url=config.base_url
            )
            plugin_available = await client.get_playback_reporting_plugin_status()
            events: list[_NormalizedEvent] = []
            if plugin_available:
                since = _last_success_from(config)
                if since is not None:
                    since -= timedelta(days=1)
                rows = await client.get_playback_reporting_events(since=since)
                events = _normalize_reporting_events(rows)
        except Exception as exc:
            error = _provider_error_message(exc)
            LOG.warning(
                "Playback Reporting history refresh failed for "
                f"{config.service_type} config {config.id}: {error}"
            )
            return await _persist_unavailable_status(
                config,
                provider=config.service_type,
                observed_service=config.service_type,
                error=error,
            )

        if not plugin_available:
            return await _persist_unavailable_status(
                config,
                provider=config.service_type,
                observed_service=config.service_type,
                error=None,
            )

        async with async_db() as session:
            db_config = await session.get(ServiceConfig, config.id)
            if db_config is None:
                raise ValueError(f"Service config {config.id} no longer exists")
            imported = await _upsert_events(
                session,
                config=db_config,
                provider=config.service_type,
                observed_service=config.service_type,
                events=events,
            )
            _set_state(
                db_config,
                provider=config.service_type,
                observed_service=config.service_type,
                available=True,
                error=None,
                imported_events=imported,
                last_event_at=max((event.played_at for event in events), default=None),
            )
            await session.commit()
        return PlaybackProviderStatus(
            config_id=config.id,
            provider=config.service_type,
            observed_service=config.service_type,
            available=True,
            imported_events=imported,
        )
    finally:
        if client is not None:
            await client.session.close()


async def _refresh_tautulli_config(
    config: ServiceConfig,
) -> PlaybackProviderStatus:
    """Refresh one Tautulli playback source."""

    client = None
    try:
        try:
            client = TautulliClient(
                api_key=_config_api_key(config), base_url=config.base_url
            )
            rows = await client.get_history_records(since=_last_success_from(config))
            events = _normalize_tautulli_events(rows)
        except Exception as exc:
            error = _provider_error_message(exc)
            LOG.warning(
                f"Tautulli history refresh failed for config {config.id}: {error}"
            )
            return await _persist_unavailable_status(
                config,
                provider=Service.TAUTULLI,
                observed_service=Service.PLEX,
                error=error,
            )

        async with async_db() as session:
            db_config = await session.get(ServiceConfig, config.id)
            if db_config is None:
                raise ValueError(f"Service config {config.id} no longer exists")
            imported = await _upsert_events(
                session,
                config=db_config,
                provider=Service.TAUTULLI,
                observed_service=Service.PLEX,
                events=events,
            )
            _set_state(
                db_config,
                provider=Service.TAUTULLI,
                observed_service=Service.PLEX,
                available=True,
                error=None,
                imported_events=imported,
                last_event_at=max((event.played_at for event in events), default=None),
            )
            await session.commit()
        return PlaybackProviderStatus(
            config_id=config.id,
            provider=Service.TAUTULLI,
            observed_service=Service.PLEX,
            available=True,
            imported_events=imported,
        )
    finally:
        if client is not None:
            await client.session.close()


async def refresh_playback_history(*, force: bool = False) -> PlaybackRefreshResult:
    """Serialize playback refreshes so providers and aggregates cannot race."""
    async with _playback_refresh_lock:
        return await _refresh_playback_history_once(force=force)


async def _refresh_playback_history_once(
    *, force: bool = False
) -> PlaybackRefreshResult:
    """Refresh configured durable playback providers and rebuild aggregates."""
    async with async_db() as session:
        configs = list(
            (
                await session.execute(
                    select(ServiceConfig).where(
                        ServiceConfig.enabled.is_(True),
                        ServiceConfig.service_type.in_(
                            [Service.JELLYFIN, Service.EMBY, Service.TAUTULLI]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        has_plex = (
            await session.scalar(
                select(ServiceConfig.id)
                .where(
                    ServiceConfig.enabled.is_(True),
                    ServiceConfig.service_type == Service.PLEX,
                )
                .limit(1)
            )
            is not None
        )

    statuses: list[PlaybackProviderStatus] = []
    refreshed = False
    for config in configs:
        if not force and (recent_status := _recent_status_from(config)) is not None:
            statuses.append(recent_status)
            continue
        if config.service_type in {Service.JELLYFIN, Service.EMBY}:
            statuses.append(await _refresh_reporting_config(config))
            refreshed = True
        elif config.service_type == Service.TAUTULLI and has_plex:
            statuses.append(await _refresh_tautulli_config(config))
            refreshed = True

    if force or refreshed:
        await rebuild_playback_history_aggregates()
    result = PlaybackRefreshResult(statuses=statuses)
    imported = sum(status.imported_events for status in statuses)
    LOG.info(
        "Playback history refresh complete: "
        f"{imported} new event(s), "
        f"{len(result.available_services)} observed service(s) available"
    )
    return result


def _aggregate_values(aggregate: PlaybackHistoryAggregate) -> dict[str, object]:
    """Convert an aggregate row into rule snapshot values."""

    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "playback.has_activity": aggregate.play_count > 0,
        "playback.play_count": aggregate.play_count,
        "playback.total_duration_minutes": aggregate.total_duration_seconds / 60,
        "playback.longest_duration_minutes": (aggregate.longest_duration_seconds / 60),
        "playback.unique_user_count": aggregate.unique_user_count,
        "playback.last_activity_at": aggregate.last_activity_at,
        "playback.days_since_last_activity": (
            (now - aggregate.last_activity_at).days
            if aggregate.last_activity_at is not None
            else None
        ),
    }


async def rebuild_playback_history_aggregates() -> None:
    """Remap retained events to current media rows and rebuild rule aggregates."""
    async with async_db() as session:
        movie_rows = (
            await session.execute(select(Movie.id, Movie.tmdb_id, Movie.added_at))
        ).all()
        movie_by_tmdb = {
            tmdb_id: (movie_id, added_at) for movie_id, tmdb_id, added_at in movie_rows
        }
        versions_by_movie: dict[int, list[int]] = defaultdict(list)
        for version_id, movie_id in (
            await session.execute(select(MovieVersion.id, MovieVersion.movie_id))
        ).all():
            versions_by_movie[movie_id].append(version_id)

        episode_by_identity: dict[
            tuple[int, int, int],
            tuple[
                int,
                int,
                int,
                datetime | None,
                datetime | None,
            ],
        ] = {}
        for (
            episode_id,
            season_id,
            series_id,
            tmdb_id,
            season_number,
            episode_number,
            series_added_at,
            season_added_at,
        ) in (
            await session.execute(
                select(
                    Episode.id,
                    Season.id,
                    Series.id,
                    Series.tmdb_id,
                    Season.season_number,
                    Episode.episode_number,
                    Series.added_at,
                    Season.added_at,
                )
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
            )
        ).all():
            episode_by_identity[(tmdb_id, season_number, episode_number)] = (
                episode_id,
                season_id,
                series_id,
                series_added_at,
                season_added_at,
            )

        aggregates: dict[PlaybackTargetKey, _Aggregate] = {}
        movie_watch: dict[int, _Aggregate] = {}
        series_watch: dict[int, _Aggregate] = {}
        season_watch: dict[int, _Aggregate] = {}
        episode_watch: dict[int, _Aggregate] = {}
        events = list(
            (await session.execute(select(PlaybackHistoryEvent))).scalars().all()
        )
        for event in events:
            target_keys: list[PlaybackTargetKey] = []
            if event.provider_media_type == "movie" and event.tmdb_id is not None:
                movie_identity = movie_by_tmdb.get(event.tmdb_id)
                if movie_identity is not None:
                    movie_id, movie_added_at = movie_identity
                    event.movie_id = movie_id
                    event.series_id = event.season_id = event.episode_id = None
                    target_keys.extend(
                        ("movie_version", version_id)
                        for version_id in versions_by_movie.get(movie_id, [])
                    )
                    if movie_added_at is None or event.played_at >= movie_added_at:
                        movie_watch.setdefault(
                            movie_id, _Aggregate(media_type=MediaType.MOVIE)
                        ).add(event)
            elif (
                event.provider_media_type == "episode"
                and event.tmdb_id is not None
                and event.season_number is not None
                and event.episode_number is not None
            ):
                identity = episode_by_identity.get(
                    (event.tmdb_id, event.season_number, event.episode_number)
                )
                if identity:
                    (
                        episode_id,
                        season_id,
                        series_id,
                        series_added_at,
                        season_added_at,
                    ) = identity
                    event.movie_id = None
                    event.episode_id = episode_id
                    event.season_id = season_id
                    event.series_id = series_id
                    target_keys.extend(
                        [
                            ("series", series_id),
                            ("season", season_id),
                            ("episode", episode_id),
                        ]
                    )
                    if series_added_at is None or event.played_at >= series_added_at:
                        series_watch.setdefault(
                            series_id, _Aggregate(media_type=MediaType.SERIES)
                        ).add(event)
                    if season_added_at is None or event.played_at >= season_added_at:
                        season_watch.setdefault(
                            season_id, _Aggregate(media_type=MediaType.SERIES)
                        ).add(event)
                        episode_watch.setdefault(
                            episode_id, _Aggregate(media_type=MediaType.SERIES)
                        ).add(event)

            for key in target_keys:
                aggregate = aggregates.setdefault(
                    key,
                    _Aggregate(
                        media_type=(
                            MediaType.MOVIE
                            if key[0] == "movie_version"
                            else MediaType.SERIES
                        )
                    ),
                )
                aggregate.add(event)

        await session.execute(delete(PlaybackHistoryAggregate))
        session.add_all(
            [
                PlaybackHistoryAggregate(
                    target_scope=scope,
                    target_id=target_id,
                    media_type=aggregate.media_type,
                    play_count=aggregate.play_count,
                    total_duration_seconds=aggregate.total_duration_seconds,
                    longest_duration_seconds=aggregate.longest_duration_seconds,
                    unique_user_count=len(aggregate.users),
                    first_activity_at=aggregate.first_activity_at,
                    last_activity_at=aggregate.last_activity_at,
                )
                for (scope, target_id), aggregate in aggregates.items()
            ]
        )

        for model, values in (
            (Movie, movie_watch),
            (Series, series_watch),
            (Season, season_watch),
            (Episode, episode_watch),
        ):
            for target_id, aggregate in values.items():
                row = cast(
                    _WatchTrackedRow | None,
                    await session.get(model, target_id),
                )
                if row is None:
                    continue
                row.view_count = max(row.view_count or 0, aggregate.play_count)
                if aggregate.last_activity_at is not None and (
                    row.last_viewed_at is None
                    or aggregate.last_activity_at > row.last_viewed_at
                ):
                    row.last_viewed_at = aggregate.last_activity_at
        await session.commit()


async def load_playback_rule_snapshot(
    db: AsyncSession,
    refresh_result: PlaybackRefreshResult,
) -> PlaybackRuleSnapshot:
    """Load aggregate values and determine which current targets were observable."""
    available_services = refresh_result.available_services
    available_targets: set[PlaybackTargetKey] = set()
    if available_services:
        movie_rows = (
            await db.execute(
                select(MovieVersion.id, MovieVersion.service)
                .join(Movie, MovieVersion.movie_id == Movie.id)
                .where(
                    MovieVersion.service.in_(available_services),
                    Movie.removed_at.is_(None),
                )
            )
        ).all()
        available_targets.update(
            ("movie_version", version_id) for version_id, _service in movie_rows
        )

        series_rows = (
            await db.execute(
                select(SeriesServiceRef.series_id)
                .join(Series, SeriesServiceRef.series_id == Series.id)
                .where(
                    SeriesServiceRef.service.in_(available_services),
                    Series.removed_at.is_(None),
                )
            )
        ).all()
        available_series_ids = {series_id for (series_id,) in series_rows}
        available_targets.update(
            ("series", series_id) for series_id in available_series_ids
        )

        season_conditions = []
        episode_conditions = []
        if Service.PLEX in available_services:
            season_conditions.append(Season.plex_season_rating_key.is_not(None))
            episode_conditions.append(Episode.plex_rating_key.is_not(None))
        if Service.JELLYFIN in available_services:
            season_conditions.append(Season.jellyfin_season_id.is_not(None))
            episode_conditions.append(Episode.jellyfin_episode_id.is_not(None))
        if Service.EMBY in available_services:
            season_conditions.append(Season.emby_season_id.is_not(None))
            episode_conditions.append(Episode.emby_episode_id.is_not(None))
        if season_conditions:
            rows = (
                await db.execute(
                    select(Season.id)
                    .join(Series, Season.series_id == Series.id)
                    .where(or_(*season_conditions), Series.removed_at.is_(None))
                )
            ).all()
            available_targets.update(("season", season_id) for (season_id,) in rows)
        if episode_conditions:
            rows = (
                await db.execute(
                    select(Episode.id)
                    .join(Season, Episode.season_id == Season.id)
                    .join(Series, Season.series_id == Series.id)
                    .where(or_(*episode_conditions), Series.removed_at.is_(None))
                )
            ).all()
            available_targets.update(("episode", episode_id) for (episode_id,) in rows)

        mapped_rows = (
            await db.execute(
                select(
                    SupplementalMediaMatch.source_service,
                    SupplementalMediaMatch.movie_id,
                    SupplementalMediaMatch.series_id,
                    SupplementalMediaMatch.season_id,
                ).where(SupplementalMediaMatch.source_service.in_(available_services))
            )
        ).all()
        mapped_movie_ids: set[int] = set()
        for _service, movie_id, series_id, season_id in mapped_rows:
            if movie_id is not None:
                mapped_movie_ids.add(movie_id)
            if series_id is not None:
                available_targets.add(("series", series_id))
            if season_id is not None:
                available_targets.add(("season", season_id))
        if mapped_movie_ids:
            version_ids = (
                await db.execute(
                    select(MovieVersion.id)
                    .join(Movie, MovieVersion.movie_id == Movie.id)
                    .where(
                        MovieVersion.movie_id.in_(mapped_movie_ids),
                        Movie.removed_at.is_(None),
                    )
                )
            ).all()
            available_targets.update(
                ("movie_version", version_id) for (version_id,) in version_ids
            )

    values_by_target = {
        (row.target_scope, row.target_id): _aggregate_values(row)
        for row in (await db.execute(select(PlaybackHistoryAggregate))).scalars().all()
    }
    zero_values: dict[str, object] = {
        "playback.has_activity": False,
        "playback.play_count": 0,
        "playback.total_duration_minutes": 0.0,
        "playback.longest_duration_minutes": 0.0,
        "playback.unique_user_count": 0,
        "playback.last_activity_at": None,
        "playback.days_since_last_activity": None,
    }
    for key in available_targets:
        values_by_target.setdefault(key, dict(zero_values))

    target_counts = {
        "movie_version": int(
            await db.scalar(
                select(func.count(MovieVersion.id))
                .join(Movie, MovieVersion.movie_id == Movie.id)
                .where(Movie.removed_at.is_(None))
            )
            or 0
        ),
        "series": int(
            await db.scalar(
                select(func.count(Series.id)).where(Series.removed_at.is_(None))
            )
            or 0
        ),
        "season": int(
            await db.scalar(
                select(func.count(Season.id))
                .join(Series, Season.series_id == Series.id)
                .where(Series.removed_at.is_(None))
            )
            or 0
        ),
        "episode": int(
            await db.scalar(
                select(func.count(Episode.id))
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .where(Series.removed_at.is_(None))
            )
            or 0
        ),
    }
    return PlaybackRuleSnapshot(
        values_by_target=values_by_target,
        available_targets=available_targets,
        target_counts=target_counts,
        errors=refresh_result.errors,
        has_configured_provider=refresh_result.has_configured_provider,
    )
