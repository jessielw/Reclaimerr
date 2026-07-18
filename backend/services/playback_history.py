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
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.core.utils.request import format_http_failure, summarize_error_message
from backend.database import async_db
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    NativePlaybackAggregate,
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
PLAYBACK_EVENT_INSERT_BATCH_SIZE = 50
PLAYBACK_EVENT_FORMAT_VERSION = 2
NATIVE_PLAYBACK_STATE_KEY = "native_playback_sync"
NATIVE_PLAYBACK_FORMAT_VERSION = 2
NATIVE_PLAYBACK_FIELDS = frozenset(
    {
        "playback.has_activity",
        "playback.play_count",
        "playback.unique_user_count",
        "playback.usernames",
        "playback.last_activity_at",
        "playback.days_since_last_activity",
    }
)
PLAYBACK_USER_FIELDS = frozenset({"playback.unique_user_count", "playback.usernames"})

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
class NativePlaybackStatus:
    """Availability of a persisted current-state media-server snapshot."""

    config_id: int
    service: Service
    available: bool
    error: str | None = None


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
    provider_statuses: list[PlaybackProviderStatus]
    unavailable_reasons: dict[str, dict[str, int]]
    unavailable_target_samples: dict[str, list[int]]
    available_fields_by_target: dict[PlaybackTargetKey, set[str]] = field(
        default_factory=dict
    )
    native_statuses: list[NativePlaybackStatus] = field(default_factory=list)
    target_ids_by_scope: dict[str, set[int]] = field(default_factory=dict)

    def unavailable_count(
        self,
        scopes: Iterable[str],
        fields: Iterable[str] | None = None,
    ) -> int:
        """Return the number of targets in the given scopes without data."""

        scope_set = set(scopes)
        requested_fields = set(fields or ())
        if not requested_fields:
            available = sum(
                1 for scope, _target_id in self.available_targets if scope in scope_set
            )
            total = sum(self.target_counts.get(scope, 0) for scope in scope_set)
            return max(0, total - available)

        return sum(
            1
            for scope, targets in self.target_ids_by_scope.items()
            if scope in scope_set
            for target_id in targets
            if not requested_fields.issubset(
                self.available_fields_by_target.get((scope, target_id), set())
            )
        )


def _playback_unavailable_reason(
    target_services: set[Service],
    refresh_result: PlaybackRefreshResult,
) -> str:
    """Explain why a current media target cannot be observed for playback rules."""

    if not refresh_result.statuses:
        return "no playback provider configured"

    failed_services = {
        status.observed_service
        for status in refresh_result.statuses
        if not status.available and status.error
    }
    unavailable_services = {
        status.observed_service
        for status in refresh_result.statuses
        if not status.available and not status.error
    }
    failed_targets = target_services & failed_services
    if failed_targets:
        services = ", ".join(sorted(service.value for service in failed_targets))
        return f"playback provider refresh failed for {services}"

    unavailable_targets = target_services & unavailable_services
    if unavailable_targets:
        services = ", ".join(sorted(service.value for service in unavailable_targets))
        return f"playback provider unavailable for {services}"

    available_services = refresh_result.available_services
    if target_services and not target_services.intersection(available_services):
        services = ", ".join(sorted(service.value for service in target_services))
        return (
            f"media service not covered by an available playback provider: {services}"
        )
    if not target_services:
        return "missing media-server identity"
    return "missing playback target mapping"


def log_playback_rule_coverage(
    snapshot: PlaybackRuleSnapshot,
    scopes: Iterable[str],
    fields: Iterable[str] | None = None,
) -> None:
    """Log observable and unknown playback-rule targets with diagnostic reasons."""

    selected_scopes = sorted(set(scopes))
    selected_fields = set(fields or ())
    provider_summary = (
        ", ".join(
            f"{status.provider.value}#{status.config_id}->{status.observed_service.value}="
            f"{'available' if status.available else 'unavailable'}"
            for status in snapshot.provider_statuses
        )
        or "none configured"
    )
    native_summary = (
        ", ".join(
            f"{status.service.value}#{status.config_id}="
            f"{'available' if status.available else 'unavailable'}"
            for status in snapshot.native_statuses
        )
        or "none configured"
    )
    coverage_parts: list[str] = []
    unknown_total = 0
    for scope in selected_scopes:
        total = snapshot.target_counts.get(scope, 0)
        target_ids = snapshot.target_ids_by_scope.get(scope, set())
        evaluable = sum(
            1
            for target_id in target_ids
            if (
                selected_fields.issubset(
                    snapshot.available_fields_by_target.get((scope, target_id), set())
                )
                if selected_fields
                else (scope, target_id) in snapshot.available_targets
            )
        )
        unknown = max(0, total - evaluable)
        unknown_total += unknown
        coverage_parts.append(
            f"{scope}: total={total}, evaluable={evaluable}, unknown={unknown}"
        )

    LOG.debug(
        "Playback rule coverage: "
        f"providers=[{provider_summary}]; native=[{native_summary}]; "
        f"{'; '.join(coverage_parts) or 'no target scopes'}"
    )
    if unknown_total == 0:
        return

    reason_parts: list[str] = []
    classified_total = 0
    for scope in selected_scopes:
        for reason, count in sorted(
            snapshot.unavailable_reasons.get(scope, {}).items()
        ):
            classified_total += count
            reason_parts.append(f"{scope}/{reason}={count}")
    LOG.warning(
        f"Playback rules have {unknown_total} unknown target(s): "
        f"{'; '.join(reason_parts) or 'reason unavailable'}"
    )
    sample_parts = [
        f"{scope}={snapshot.unavailable_target_samples.get(scope, [])}"
        for scope in selected_scopes
        if snapshot.unavailable_target_samples.get(scope)
    ]
    if sample_parts:
        LOG.debug("Playback unknown target ID samples: " + "; ".join(sample_parts))
    if classified_total != unknown_total:
        LOG.debug(
            "Playback coverage reasons are aggregated across fields: "
            f"unknown={unknown_total}, classified={classified_total}"
        )


@dataclass(slots=True)
class _NormalizedEvent:
    """Normalized playback event ready for database upsert."""

    source_event_key: str
    source_item_id: str
    provider_media_type: Literal["movie", "episode"]
    played_at: datetime
    duration_seconds: int
    source_user_id: str | None
    source_username: str | None
    completed: bool | None = None


@dataclass(slots=True)
class _Aggregate:
    """In-memory accumulator for playback history aggregates."""

    media_type: MediaType
    play_count: int = 0
    total_duration_seconds: int = 0
    longest_duration_seconds: int = 0
    users: set[str] = field(default_factory=set)
    usernames: dict[str, str] = field(default_factory=dict)
    usernames_complete: bool = True
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
            username = str(event.source_username or "").strip()
            if username:
                self.usernames.setdefault(username.casefold(), username)
            else:
                self.usernames_complete = False
        else:
            self.usernames_complete = False
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
    state = _state_from(config)
    if config.service_type is Service.TAUTULLI:
        version = _parse_int(state.get("format_version")) or 0
        if version < PLAYBACK_EVENT_FORMAT_VERSION:
            return None
    return _parse_datetime(state.get("last_success_at"))


def _recent_status_from(config: ServiceConfig) -> PlaybackProviderStatus | None:
    """Return cached provider status if the last attempt is still fresh."""

    state = _state_from(config)
    if config.service_type is Service.TAUTULLI:
        version = _parse_int(state.get("format_version")) or 0
        if version < PLAYBACK_EVENT_FORMAT_VERSION and bool(state.get("available")):
            return None
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
        "format_version": (
            PLAYBACK_EVENT_FORMAT_VERSION
            if available
            else previous.get("format_version", 0)
        ),
    }
    settings[PLAYBACK_HISTORY_STATE_KEY] = state
    config.extra_settings = settings


def _normalize_reporting_events(
    rows: Iterable[Mapping[str, object]],
    usernames_by_id: Mapping[str, str] | None = None,
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
        username = usernames_by_id.get(user_id) if user_id and usernames_by_id else None
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
                source_username=username,
                completed=None,
            )
        )
    return events


def _normalize_tautulli_events(
    rows: Iterable[Mapping[str, object]],
    usernames_by_id: Mapping[str, str] | None = None,
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
        username = (
            str(
                (usernames_by_id.get(user_id) if user_id and usernames_by_id else "")
                or row.get("user")
                or row.get("friendly_name")
                or ""
            ).strip()
            or None
        )
        if duration is None or duration < threshold or played_at is None or not item_id:
            continue
        watched_status = _parse_int(row.get("watched_status"))
        completed = watched_status == 1 if watched_status is not None else None
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
                source_username=username,
                completed=completed,
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

    events_by_key: dict[str, _NormalizedEvent] = {}
    for event in events:
        events_by_key[event.source_event_key] = event
    duplicate_count = len(events) - len(events_by_key)
    if duplicate_count:
        LOG.debug(
            f"Deduplicated {duplicate_count} playback history event(s) from "
            f"{provider.value} config {config.id}"
        )
    events = list(events_by_key.values())

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

    new_values: list[dict[str, object]] = []
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
            new_values.append(
                {
                    "source_service": provider,
                    "source_service_config_id": config.id,
                    "source_event_key": event.source_event_key,
                    "source_item_id": event.source_item_id,
                    "provider_media_type": event.provider_media_type,
                    "played_at": event.played_at,
                    "duration_seconds": event.duration_seconds,
                    "completed": event.completed,
                    "source_user_id": event.source_user_id,
                    "source_username": event.source_username,
                    "tmdb_id": tmdb_id,
                    "season_number": season_number,
                    "episode_number": episode_number,
                    "movie_id": movie_id,
                    "series_id": series_id,
                    "season_id": season_id,
                    "episode_id": episode_id,
                }
            )
            continue

        existing.source_item_id = event.source_item_id
        existing.provider_media_type = event.provider_media_type
        existing.played_at = event.played_at
        existing.duration_seconds = event.duration_seconds
        existing.completed = event.completed
        existing.source_user_id = event.source_user_id
        if event.source_username:
            existing.source_username = event.source_username
        if tmdb_id is not None:
            existing.tmdb_id = tmdb_id
            existing.season_number = season_number
            existing.episode_number = episode_number
            existing.movie_id = movie_id
            existing.series_id = series_id
            existing.season_id = season_id
            existing.episode_id = episode_id
    return await _insert_new_events(session, new_values)


async def _backfill_event_usernames(
    session: AsyncSession,
    *,
    config_id: int,
    usernames_by_id: Mapping[str, str],
) -> None:
    """Apply current provider usernames to every retained event for a config."""

    if not usernames_by_id:
        return
    events = (
        (
            await session.execute(
                select(PlaybackHistoryEvent).where(
                    PlaybackHistoryEvent.source_service_config_id == config_id,
                    PlaybackHistoryEvent.source_user_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for event in events:
        username = usernames_by_id.get(str(event.source_user_id or ""))
        if username:
            event.source_username = username


async def _insert_new_events(
    session: AsyncSession, values: list[dict[str, object]]
) -> int:
    """Insert playback events while treating uniqueness conflicts as existing rows."""

    inserted = 0
    for offset in range(0, len(values), PLAYBACK_EVENT_INSERT_BATCH_SIZE):
        batch = values[offset : offset + PLAYBACK_EVENT_INSERT_BATCH_SIZE]
        statement = (
            sqlite_insert(PlaybackHistoryEvent)
            .values(batch)
            .on_conflict_do_nothing(
                index_elements=[
                    PlaybackHistoryEvent.source_service_config_id,
                    PlaybackHistoryEvent.source_event_key,
                ]
            )
            .returning(PlaybackHistoryEvent.source_event_key)
        )
        result = await session.execute(statement)
        inserted += len(result.scalars().all())
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
            usernames_by_id: dict[str, str] = {}
            if plugin_available:
                try:
                    usernames_by_id = {
                        str(user.id): str(user.name).strip()
                        for user in await client.get_users()
                        if str(user.id).strip() and str(user.name).strip()
                    }
                except Exception as exc:
                    LOG.warning(
                        "Playback username lookup failed for "
                        f"{config.service_type} config {config.id}: "
                        f"{_provider_error_message(exc)}"
                    )
                since = _last_success_from(config)
                if since is not None:
                    since -= timedelta(days=1)
                rows = await client.get_playback_reporting_events(since=since)
                events = _normalize_reporting_events(rows, usernames_by_id)
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
            await _backfill_event_usernames(
                session,
                config_id=db_config.id,
                usernames_by_id=usernames_by_id,
            )
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
            usernames_by_id: dict[str, str] = {}
            try:
                usernames_by_id = {
                    str(row.get("user_id") or "").strip(): str(
                        row.get("username") or row.get("friendly_name") or ""
                    ).strip()
                    for row in await client.get_users()
                    if str(row.get("user_id") or "").strip()
                    and str(
                        row.get("username") or row.get("friendly_name") or ""
                    ).strip()
                }
            except Exception as exc:
                LOG.warning(
                    "Playback username lookup failed for Tautulli config "
                    f"{config.id}: {_provider_error_message(exc)}"
                )
            rows = await client.get_history_records(since=_last_success_from(config))
            events = _normalize_tautulli_events(rows, usernames_by_id)
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
            await _backfill_event_usernames(
                session,
                config_id=db_config.id,
                usernames_by_id=usernames_by_id,
            )
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
    values: dict[str, object] = {
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
    if aggregate.usernames_complete:
        values["playback.usernames"] = list(aggregate.usernames or [])
    return values


def _native_aggregate_values(aggregate: NativePlaybackAggregate) -> dict[str, object]:
    """Convert persisted current media-server state into supported rule values."""

    now = datetime.now(UTC).replace(tzinfo=None)
    values: dict[str, object] = {
        "playback.has_activity": aggregate.has_activity,
        "playback.play_count": aggregate.play_count,
        "playback.unique_user_count": aggregate.unique_user_count,
    }
    if aggregate.last_activity_at is not None or not aggregate.has_activity:
        values["playback.last_activity_at"] = aggregate.last_activity_at
        values["playback.days_since_last_activity"] = (
            (now - aggregate.last_activity_at).days
            if aggregate.last_activity_at is not None
            else None
        )
    if aggregate.usernames_complete:
        values["playback.usernames"] = list(aggregate.usernames or [])
    return values


def _playback_count_value(raw: object) -> int:
    return max(0, _parse_int(raw) or 0)


def _merge_playback_source_values(
    plugin_values: Mapping[str, object] | None,
    native_values: Mapping[str, object] | None,
) -> dict[str, object]:
    """Merge like-for-like values from multiple observations of one source."""

    plugin_values = plugin_values or {}
    native_values = native_values or {}
    values: dict[str, object] = dict(plugin_values)

    for field in NATIVE_PLAYBACK_FIELDS:
        if field not in native_values:
            continue
        if field not in values:
            values[field] = native_values[field]

    if (
        "playback.has_activity" in plugin_values
        or "playback.has_activity" in native_values
    ):
        values["playback.has_activity"] = bool(
            plugin_values.get("playback.has_activity", False)
            or native_values.get("playback.has_activity", False)
        )

    if "playback.play_count" in plugin_values or "playback.play_count" in native_values:
        values["playback.play_count"] = max(
            _playback_count_value(plugin_values.get("playback.play_count", 0)),
            _playback_count_value(native_values.get("playback.play_count", 0)),
        )

    plugin_users = plugin_values.get("playback.usernames")
    native_users = native_values.get("playback.usernames")
    if isinstance(plugin_users, list) or isinstance(native_users, list):
        merged_users: dict[str, str] = {}
        for users in (plugin_users, native_users):
            if not isinstance(users, list):
                continue
            for raw_name in users:
                name = str(raw_name).strip()
                if name:
                    merged_users.setdefault(name.casefold(), name)
        values["playback.usernames"] = sorted(merged_users.values(), key=str.casefold)

    if (
        "playback.unique_user_count" in plugin_values
        or "playback.unique_user_count" in native_values
    ):
        resolved_users = values.get("playback.usernames")
        resolved_count = len(resolved_users) if isinstance(resolved_users, list) else 0
        values["playback.unique_user_count"] = max(
            _playback_count_value(plugin_values.get("playback.unique_user_count", 0)),
            _playback_count_value(native_values.get("playback.unique_user_count", 0)),
            resolved_count,
        )

    plugin_last = plugin_values.get("playback.last_activity_at")
    native_last = native_values.get("playback.last_activity_at")
    latest_values = [
        value for value in (plugin_last, native_last) if isinstance(value, datetime)
    ]
    if latest_values or (
        "playback.last_activity_at" in plugin_values
        or "playback.last_activity_at" in native_values
    ):
        latest = max(latest_values) if latest_values else None
        values["playback.last_activity_at"] = latest
        values["playback.days_since_last_activity"] = (
            (datetime.now(UTC).replace(tzinfo=None) - latest).days
            if latest is not None
            else None
        )

    return values


def _merge_playback_metric_values(
    plugin_values: Mapping[str, object] | None,
    native_values: Mapping[str, object] | None,
) -> dict[str, object]:
    """Merge historical activity metrics without deciding current user state."""

    plugin_values = plugin_values or {}
    native_values = native_values or {}
    values = {
        field: value
        for field, value in plugin_values.items()
        if field not in PLAYBACK_USER_FIELDS
    }

    for field in NATIVE_PLAYBACK_FIELDS - PLAYBACK_USER_FIELDS:
        if field in native_values and field not in values:
            values[field] = native_values[field]

    if (
        "playback.has_activity" in plugin_values
        or "playback.has_activity" in native_values
    ):
        values["playback.has_activity"] = bool(
            plugin_values.get("playback.has_activity", False)
            or native_values.get("playback.has_activity", False)
        )

    if "playback.play_count" in plugin_values or "playback.play_count" in native_values:
        values["playback.play_count"] = max(
            _playback_count_value(plugin_values.get("playback.play_count", 0)),
            _playback_count_value(native_values.get("playback.play_count", 0)),
        )

    plugin_last = plugin_values.get("playback.last_activity_at")
    native_last = native_values.get("playback.last_activity_at")
    latest_values = [
        value for value in (plugin_last, native_last) if isinstance(value, datetime)
    ]
    if latest_values or (
        "playback.last_activity_at" in plugin_values
        or "playback.last_activity_at" in native_values
    ):
        latest = max(latest_values) if latest_values else None
        values["playback.last_activity_at"] = latest
        values["playback.days_since_last_activity"] = (
            (datetime.now(UTC).replace(tzinfo=None) - latest).days
            if latest is not None
            else None
        )

    return values


def _resolve_playback_user_values(
    plugin_values: Mapping[str, object] | None,
    native_values: Mapping[str, object] | None,
    *,
    native_current_state_available: bool,
    native_current_state_unknown: bool,
) -> dict[str, object]:
    """Return user fields from the one source authoritative for this target."""

    if native_current_state_unknown:
        return {}
    source_values = native_values if native_current_state_available else plugin_values
    if source_values is None:
        return {}
    return {
        field: source_values[field]
        for field in PLAYBACK_USER_FIELDS
        if field in source_values
    }


def _native_status_from_config(config: ServiceConfig) -> NativePlaybackStatus:
    """Read the availability state written by the native snapshot refresh."""

    extra_settings = config.extra_settings
    state = (
        extra_settings.get(NATIVE_PLAYBACK_STATE_KEY)
        if isinstance(extra_settings, dict)
        else None
    )
    if not isinstance(state, dict):
        return NativePlaybackStatus(
            config_id=config.id,
            service=config.service_type,
            available=False,
            error="native playback state has not been synced yet",
        )
    if state.get("format_version") != NATIVE_PLAYBACK_FORMAT_VERSION:
        return NativePlaybackStatus(
            config_id=config.id,
            service=config.service_type,
            available=False,
            error="native playback state requires a playback data refresh",
        )
    if state.get("available") is True:
        return NativePlaybackStatus(
            config_id=config.id,
            service=config.service_type,
            available=True,
        )
    error = state.get("last_error")
    return NativePlaybackStatus(
        config_id=config.id,
        service=config.service_type,
        available=False,
        error=str(error) if error else "native playback snapshot is unavailable",
    )


async def _load_native_playback_statuses(
    db: AsyncSession,
) -> list[NativePlaybackStatus]:
    configs = (
        (
            await db.execute(
                select(ServiceConfig).where(
                    ServiceConfig.enabled.is_(True),
                    ServiceConfig.service_type.in_({Service.JELLYFIN, Service.EMBY}),
                )
            )
        )
        .scalars()
        .all()
    )
    return [_native_status_from_config(config) for config in configs]


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
                    usernames=sorted(aggregate.usernames.values(), key=str.casefold),
                    usernames_complete=aggregate.usernames_complete,
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
    """Merge durable events with persisted native current media-server state."""
    target_services: dict[str, dict[int, set[Service]]] = {
        scope: {} for scope in PLAYBACK_TARGET_SCOPES
    }
    movie_identity_rows = (
        await db.execute(
            select(MovieVersion.id, MovieVersion.service)
            .join(Movie, MovieVersion.movie_id == Movie.id)
            .where(Movie.removed_at.is_(None))
        )
    ).all()
    target_services["movie_version"] = {
        version_id: {service} for version_id, service in movie_identity_rows
    }

    active_series_ids = {
        series_id
        for (series_id,) in (
            await db.execute(select(Series.id).where(Series.removed_at.is_(None)))
        ).all()
    }
    series_services: dict[int, set[Service]] = {
        series_id: set() for series_id in active_series_ids
    }
    series_identity_rows = (
        await db.execute(
            select(SeriesServiceRef.series_id, SeriesServiceRef.service)
            .join(Series, SeriesServiceRef.series_id == Series.id)
            .where(Series.removed_at.is_(None))
        )
    ).all()
    for series_id, service in series_identity_rows:
        series_services[series_id].add(service)
    target_services["series"] = series_services

    season_identity_rows = (
        await db.execute(
            select(
                Season.id,
                Season.plex_season_rating_key,
                Season.jellyfin_season_id,
                Season.emby_season_id,
            )
            .join(Series, Season.series_id == Series.id)
            .where(Series.removed_at.is_(None))
        )
    ).all()
    target_services["season"] = {
        season_id: {
            service
            for service, identity in (
                (Service.PLEX, plex_id),
                (Service.JELLYFIN, jellyfin_id),
                (Service.EMBY, emby_id),
            )
            if identity
        }
        for season_id, plex_id, jellyfin_id, emby_id in season_identity_rows
    }

    episode_identity_rows = (
        await db.execute(
            select(
                Episode.id,
                Episode.plex_rating_key,
                Episode.jellyfin_episode_id,
                Episode.emby_episode_id,
            )
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .where(Series.removed_at.is_(None))
        )
    ).all()
    target_services["episode"] = {
        episode_id: {
            service
            for service, identity in (
                (Service.PLEX, plex_id),
                (Service.JELLYFIN, jellyfin_id),
                (Service.EMBY, emby_id),
            )
            if identity
        }
        for episode_id, plex_id, jellyfin_id, emby_id in episode_identity_rows
    }

    supplemental_rows = (
        await db.execute(
            select(
                SupplementalMediaMatch.source_service,
                SupplementalMediaMatch.movie_id,
                SupplementalMediaMatch.series_id,
                SupplementalMediaMatch.season_id,
            )
        )
    ).all()
    supplemental_movie_ids: dict[Service, set[int]] = defaultdict(set)
    for service, movie_id, series_id, season_id in supplemental_rows:
        if movie_id is not None:
            supplemental_movie_ids[service].add(movie_id)
        if series_id is not None:
            target_services["series"].setdefault(series_id, set()).add(service)
        if season_id is not None:
            target_services["season"].setdefault(season_id, set()).add(service)
    for service, movie_ids in supplemental_movie_ids.items():
        version_rows = (
            await db.execute(
                select(MovieVersion.id)
                .join(Movie, MovieVersion.movie_id == Movie.id)
                .where(
                    MovieVersion.movie_id.in_(movie_ids),
                    Movie.removed_at.is_(None),
                )
            )
        ).all()
        for (version_id,) in version_rows:
            target_services["movie_version"].setdefault(version_id, set()).add(service)

    native_statuses = await _load_native_playback_statuses(db)
    native_available_config_ids = {
        status.config_id for status in native_statuses if status.available
    }
    native_available_services = {
        status.service for status in native_statuses if status.available
    }
    plugin_available_services = refresh_result.available_services

    plugin_rows = (await db.execute(select(PlaybackHistoryAggregate))).scalars().all()
    plugin_values_by_target = {
        (row.target_scope, row.target_id): _aggregate_values(row) for row in plugin_rows
    }
    incomplete_plugin_username_targets: set[PlaybackTargetKey] = {
        (row.target_scope, row.target_id)
        for row in plugin_rows
        if not row.usernames_complete
    }
    incomplete_timestamp_targets: set[PlaybackTargetKey] = set()
    incomplete_native_username_targets: set[PlaybackTargetKey] = set()
    native_values_by_target: dict[PlaybackTargetKey, dict[str, object]] = {}
    if native_available_config_ids:
        native_rows = (
            (
                await db.execute(
                    select(NativePlaybackAggregate).where(
                        NativePlaybackAggregate.source_service_config_id.in_(
                            native_available_config_ids
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in native_rows:
            key = (row.target_scope, row.target_id)
            if not row.usernames_complete:
                incomplete_native_username_targets.add(key)
            if row.has_activity and row.last_activity_at is None:
                incomplete_timestamp_targets.add(key)
            native_values_by_target[key] = _merge_playback_source_values(
                native_values_by_target.get(key),
                _native_aggregate_values(row),
            )

    plugin_zero_values: dict[str, object] = {
        "playback.has_activity": False,
        "playback.play_count": 0,
        "playback.total_duration_minutes": 0.0,
        "playback.longest_duration_minutes": 0.0,
        "playback.unique_user_count": 0,
        "playback.usernames": [],
        "playback.last_activity_at": None,
        "playback.days_since_last_activity": None,
    }
    native_zero_values = {
        field: value
        for field, value in plugin_zero_values.items()
        if field in NATIVE_PLAYBACK_FIELDS
    }
    plugin_covered_targets: set[PlaybackTargetKey] = set(plugin_values_by_target)
    native_covered_targets: set[PlaybackTargetKey] = set(native_values_by_target)
    for scope, services_by_target in target_services.items():
        for target_id, services in services_by_target.items():
            key = (scope, target_id)
            if services & plugin_available_services:
                plugin_covered_targets.add(key)
            if services & native_available_services:
                native_covered_targets.add(key)

    available_fields_by_target: dict[PlaybackTargetKey, set[str]] = {}
    for key, values in plugin_values_by_target.items():
        available_fields_by_target.setdefault(key, set()).update(values)
    for key in plugin_covered_targets:
        if key not in plugin_values_by_target:
            plugin_values_by_target[key] = dict(plugin_zero_values)
            available_fields_by_target.setdefault(key, set()).update(plugin_zero_values)
    for key, values in native_values_by_target.items():
        available_fields_by_target.setdefault(key, set()).update(
            field for field in values if field in NATIVE_PLAYBACK_FIELDS
        )
    for key in native_covered_targets:
        if key not in native_values_by_target:
            native_values_by_target[key] = dict(native_zero_values)
            available_fields_by_target.setdefault(key, set()).update(native_zero_values)

    # A failed or out-of-date Jellyfin/Emby snapshot cannot safely be replaced
    # with imported event users: those are historical activity, not current
    # watched state. Keep other playback metrics available, but make user
    # conditions unknown until the next successful playback data refresh.
    native_user_unknown_targets: set[PlaybackTargetKey] = set()
    for scope, services_by_target in target_services.items():
        for target_id, services in services_by_target.items():
            relevant_native_statuses = [
                status for status in native_statuses if status.service in services
            ]
            if relevant_native_statuses and not any(
                status.available for status in relevant_native_statuses
            ):
                native_user_unknown_targets.add((scope, target_id))

    values_by_target: dict[PlaybackTargetKey, dict[str, object]] = {}
    for key in plugin_values_by_target.keys() | native_values_by_target.keys():
        values = _merge_playback_metric_values(
            plugin_values_by_target.get(key), native_values_by_target.get(key)
        )
        values.update(
            _resolve_playback_user_values(
                plugin_values_by_target.get(key),
                native_values_by_target.get(key),
                native_current_state_available=key in native_covered_targets,
                native_current_state_unknown=key in native_user_unknown_targets,
            )
        )
        values_by_target[key] = values

    # Usernames require complete identity resolution from whichever source is
    # authoritative for that target. A count can remain available without every
    # display name, so only the username list is made unknown here.
    incomplete_username_targets = (
        incomplete_plugin_username_targets - native_covered_targets
    ) | (incomplete_native_username_targets & native_covered_targets)
    for key in incomplete_username_targets:
        values_by_target.get(key, {}).pop("playback.usernames", None)
        available_fields_by_target.get(key, set()).discard("playback.usernames")
    for key in native_user_unknown_targets:
        values_by_target.get(key, {}).pop("playback.usernames", None)
        values_by_target.get(key, {}).pop("playback.unique_user_count", None)
        available_fields = available_fields_by_target.get(key, set())
        available_fields.discard("playback.usernames")
        available_fields.discard("playback.unique_user_count")
    for key in incomplete_timestamp_targets:
        values = values_by_target.get(key, {})
        values.pop("playback.last_activity_at", None)
        values.pop("playback.days_since_last_activity", None)
        available_fields = available_fields_by_target.get(key, set())
        available_fields.discard("playback.last_activity_at")
        available_fields.discard("playback.days_since_last_activity")
    available_targets = set(values_by_target)

    unavailable_reasons: dict[str, dict[str, int]] = {}
    unavailable_target_samples: dict[str, list[int]] = {}
    for scope, services_by_target in target_services.items():
        reason_counts: defaultdict[str, int] = defaultdict(int)
        unknown_ids: list[int] = []
        for target_id, services in services_by_target.items():
            if (scope, target_id) in available_targets:
                continue
            native_failures = [
                status
                for status in native_statuses
                if status.service in services and not status.available
            ]
            if native_failures:
                reason = "; ".join(
                    sorted(
                        status.error or "native playback snapshot unavailable"
                        for status in native_failures
                    )
                )
            else:
                reason = _playback_unavailable_reason(services, refresh_result)
            reason_counts[reason] += 1
            unknown_ids.append(target_id)
        unavailable_reasons[scope] = dict(reason_counts)
        unavailable_target_samples[scope] = sorted(unknown_ids)[:10]

    target_counts = {
        scope: len(services_by_target)
        for scope, services_by_target in target_services.items()
    }
    target_ids_by_scope = {
        scope: set(services_by_target)
        for scope, services_by_target in target_services.items()
    }
    native_errors = [
        status.error
        for status in native_statuses
        if not status.available and status.error
    ]
    return PlaybackRuleSnapshot(
        values_by_target=values_by_target,
        available_targets=available_targets,
        target_counts=target_counts,
        errors=[*refresh_result.errors, *native_errors],
        has_configured_provider=(
            refresh_result.has_configured_provider or bool(native_statuses)
        ),
        provider_statuses=list(refresh_result.statuses),
        unavailable_reasons=unavailable_reasons,
        unavailable_target_samples=unavailable_target_samples,
        available_fields_by_target=available_fields_by_target,
        native_statuses=native_statuses,
        target_ids_by_scope=target_ids_by_scope,
    )
