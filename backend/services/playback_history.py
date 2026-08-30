from __future__ import annotations

import hashlib
from asyncio import Lock
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

from cryptography.fernet import InvalidToken
from sqlalchemy import and_, delete, false, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.core.utils.request import format_http_failure, summarize_error_message
from backend.database import async_db
from backend.database.models import (
    Episode,
    GeneralSettings,
    Movie,
    MovieVersion,
    NativePlaybackAggregate,
    PlaybackHistoryAggregate,
    PlaybackHistoryEvent,
    PlaybackHistoryUserAggregate,
    Season,
    Series,
    SeriesServiceRef,
    ServiceConfig,
    SupplementalMediaMatch,
)
from backend.enums import MediaType, Service
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.media_identity import (
    MediaIdentityOwnership,
    load_media_identity_ownership,
)
from backend.services.tautulli import TautulliClient
from backend.services.tracearr import TracearrClient

PLAYBACK_HISTORY_STATE_KEY = "playback_history_sync"
PLAYBACK_MOVIE_MIN_SECONDS = 15
PLAYBACK_EPISODE_MIN_SECONDS = 7
PLAYBACK_TARGET_SCOPES = ("movie_version", "series", "season", "episode")
PLAYBACK_PREVIEW_REFRESH_INTERVAL = timedelta(minutes=5)
PLAYBACK_EVENT_INSERT_BATCH_SIZE = 50
PLAYBACK_EVENT_FORMAT_VERSION = 2
# 2: events resolve against the server they were observed on rather than every
# server of that type, so retained history has to be re-resolved and rebuilt.
PLAYBACK_AGGREGATE_FORMAT_VERSION = 2
TRACEARR_PLAYBACK_FORMAT_VERSION = 2
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
# fields only imported Playback Reporting/Tautulli events can answer: native
# media-server watch state records that something was watched, not for how long.
# A target carrying one of these has imported history behind it, which is also
# what the per-user duration and percent fields are built from.
IMPORTED_PLAYBACK_DURATION_FIELDS = frozenset(
    {
        "playback.total_duration_minutes",
        "playback.longest_duration_minutes",
    }
)

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
    # Which media-server config this provider observes. One Tracearr config can
    # bind several servers of the same type, so without this two bindings render
    # identically ("tracearr#6->plex" twice) in the coverage log.
    observed_config_id: int | None = None


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
            f"{status.provider.value}#{status.config_id}->{status.observed_service.value}"
            f"{f'#{status.observed_config_id}' if status.observed_config_id else ''}="
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


@dataclass(frozen=True, slots=True)
class _TracearrBinding:
    """One Tracearr server bound to one configured Reclaimerr media server."""

    service_config_id: int
    tracearr_server_id: str
    tracearr_server_name: str
    observed_service: Service


def _tracearr_bindings(config: ServiceConfig) -> list[_TracearrBinding]:
    settings = config.extra_settings if isinstance(config.extra_settings, dict) else {}
    raw_bindings = settings.get("server_bindings")
    if not isinstance(raw_bindings, list):
        return []

    bindings: list[_TracearrBinding] = []
    seen_local: set[int] = set()
    seen_remote: set[str] = set()
    for raw in raw_bindings:
        if not isinstance(raw, Mapping):
            continue
        config_id = _parse_int(raw.get("service_config_id"))
        server_id = str(raw.get("tracearr_server_id") or "").strip()
        server_name = str(raw.get("tracearr_server_name") or server_id).strip()
        try:
            observed_service = Service(str(raw.get("server_type") or "").lower())
        except ValueError:
            continue
        if (
            config_id is None
            or not server_id
            or observed_service not in {Service.PLEX, Service.JELLYFIN, Service.EMBY}
            or config_id in seen_local
            or server_id in seen_remote
        ):
            continue
        seen_local.add(config_id)
        seen_remote.add(server_id)
        bindings.append(
            _TracearrBinding(
                service_config_id=config_id,
                tracearr_server_id=server_id,
                tracearr_server_name=server_name or server_id,
                observed_service=observed_service,
            )
        )
    return bindings


async def load_active_tracearr_sources(
    db: AsyncSession,
) -> set[tuple[int, Service, str]]:
    """Return active Tracearr config/service/server durable-source bindings."""

    configs = list(
        (
            await db.execute(
                select(ServiceConfig).where(
                    ServiceConfig.enabled.is_(True),
                    ServiceConfig.service_type.in_(
                        {Service.TRACEARR, Service.PLEX, Service.JELLYFIN, Service.EMBY}
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    media_configs = {
        config.id: config
        for config in configs
        if config.service_type in {Service.PLEX, Service.JELLYFIN, Service.EMBY}
    }
    active: set[tuple[int, Service, str]] = set()
    for config in configs:
        if config.service_type is not Service.TRACEARR:
            continue
        for binding in _tracearr_bindings(config):
            media_config = media_configs.get(binding.service_config_id)
            if (
                media_config is not None
                and media_config.service_type is binding.observed_service
            ):
                active.add(
                    (
                        config.id,
                        binding.observed_service,
                        binding.tracearr_server_id,
                    )
                )
    return active


def active_playback_event_clause(
    active_tracearr_sources: set[tuple[int, Service, str]],
):
    """Build the SQL predicate for the single authoritative durable provider."""

    selected_services = {
        service for _config_id, service, _server_id in active_tracearr_sources
    }
    legacy_clause = and_(
        PlaybackHistoryEvent.source_service != Service.TRACEARR,
        PlaybackHistoryEvent.observed_service.not_in(selected_services),
    )
    tracearr_clauses = [
        and_(
            PlaybackHistoryEvent.source_service == Service.TRACEARR,
            PlaybackHistoryEvent.source_service_config_id == config_id,
            PlaybackHistoryEvent.observed_service == service,
            PlaybackHistoryEvent.source_event_key.startswith(f"{server_id}:"),
        )
        for config_id, service, server_id in active_tracearr_sources
    ]
    return or_(legacy_clause, or_(*tracearr_clauses) if tracearr_clauses else false())


def completion_evidence_event_clause(
    active_tracearr_sources: set[tuple[int, Service, str]],
):
    """Build the SQL predicate for durable proof that a play finished.

    Aggregates must pick one provider per media server so plays are not counted
    twice. Completion is a boolean instead: a finished play a superseded
    provider recorded is still proof the person watched the item. Binding
    Tracearr to a Plex server would otherwise retire the Tautulli history that
    predates Tracearr, which is invisible and looks like the media was never
    watched.

    Tracearr rows from bindings that are no longer active are still excluded --
    those point at servers this install no longer tracks.
    """

    tracearr_clauses = [
        and_(
            PlaybackHistoryEvent.source_service == Service.TRACEARR,
            PlaybackHistoryEvent.source_service_config_id == config_id,
            PlaybackHistoryEvent.observed_service == service,
            PlaybackHistoryEvent.source_event_key.startswith(f"{server_id}:"),
        )
        for config_id, service, server_id in active_tracearr_sources
    ]
    non_tracearr_clause = PlaybackHistoryEvent.source_service != Service.TRACEARR
    if not tracearr_clauses:
        return non_tracearr_clause
    return or_(non_tracearr_clause, or_(*tracearr_clauses))


def _playback_event_is_active(
    event: PlaybackHistoryEvent,
    active_tracearr_sources: set[tuple[int, Service, str]],
) -> bool:
    observed_service = _observed_service_for_event(event)
    selected_services = {
        service for _config_id, service, _server_id in active_tracearr_sources
    }
    if event.source_service is Service.TRACEARR:
        return any(
            event.source_service_config_id == config_id
            and observed_service is service
            and event.source_event_key.startswith(f"{server_id}:")
            for config_id, service, server_id in active_tracearr_sources
        )
    return observed_service not in selected_services


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


@dataclass(slots=True)
class _UserAggregate:
    """Per-source user identities for one playback target."""

    media_type: MediaType
    users: set[str] = field(default_factory=set)
    usernames: dict[str, str] = field(default_factory=dict)
    usernames_complete: bool = True

    def add(self, event: PlaybackHistoryEvent) -> None:
        """Merge one playback event into the source-scoped user aggregate."""

        if not event.source_user_id:
            self.usernames_complete = False
            return
        self.users.add(str(event.source_user_id))
        username = str(event.source_username or "").strip()
        if username:
            self.usernames.setdefault(username.casefold(), username)
        else:
            self.usernames_complete = False


@dataclass(slots=True)
class _UserEvidence:
    """Merged authoritative user values for one source or target."""

    unique_user_count: int = 0
    usernames: dict[str, str] = field(default_factory=dict)
    usernames_complete: bool = True

    def merge(
        self,
        *,
        unique_user_count: int,
        usernames: Iterable[object],
        usernames_complete: bool,
    ) -> None:
        """Merge one source while deduplicating display names case-insensitively."""

        self.unique_user_count = max(self.unique_user_count, unique_user_count)
        for raw_name in usernames:
            name = str(raw_name or "").strip()
            if name:
                self.usernames.setdefault(name.casefold(), name)
        self.usernames_complete = self.usernames_complete and usernames_complete

    def values(self) -> dict[str, object]:
        """Return rule fields represented by this evidence."""

        values: dict[str, object] = {
            "playback.unique_user_count": max(
                self.unique_user_count,
                len(self.usernames),
            )
        }
        if self.usernames_complete:
            values["playback.usernames"] = sorted(
                self.usernames.values(),
                key=str.casefold,
            )
        return values


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
    aggregate_version = _parse_int(state.get("aggregate_format_version")) or 0
    if aggregate_version < PLAYBACK_AGGREGATE_FORMAT_VERSION:
        return None
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
        "aggregate_format_version": previous.get("aggregate_format_version", 0),
    }
    settings[PLAYBACK_HISTORY_STATE_KEY] = state
    config.extra_settings = settings


async def _mark_aggregate_format_current(config_ids: Iterable[int]) -> None:
    """Record a successful source-aware aggregate rebuild."""

    unique_ids = set(config_ids)
    if not unique_ids:
        return
    async with async_db() as session:
        configs = (
            (
                await session.execute(
                    select(ServiceConfig).where(ServiceConfig.id.in_(unique_ids))
                )
            )
            .scalars()
            .all()
        )
        for config in configs:
            settings = (
                dict(config.extra_settings)
                if isinstance(config.extra_settings, dict)
                else {}
            )
            state = dict(_state_from(config))
            state["aggregate_format_version"] = PLAYBACK_AGGREGATE_FORMAT_VERSION
            settings[PLAYBACK_HISTORY_STATE_KEY] = state
            config.extra_settings = settings
        await session.commit()


def _normalize_reporting_events(
    rows: Iterable[Mapping[str, object]],
    usernames_by_id: Mapping[str, str] | None = None,
    *,
    movie_min_seconds: int = PLAYBACK_MOVIE_MIN_SECONDS,
    episode_min_seconds: int = PLAYBACK_EPISODE_MIN_SECONDS,
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
        threshold = movie_min_seconds if media_type == "movie" else episode_min_seconds
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
    *,
    movie_min_seconds: int = PLAYBACK_MOVIE_MIN_SECONDS,
    episode_min_seconds: int = PLAYBACK_EPISODE_MIN_SECONDS,
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
        threshold = movie_min_seconds if media_type == "movie" else episode_min_seconds
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


def _coerce_completed(value: object) -> bool | None:
    """Interpret a provider watched flag that may not be a JSON boolean.

    Tracearr is the only admitted durable source for a bound Plex server, so a
    flag arriving as 1, "true", or "yes" used to erase every completion signal
    for that server rather than just that one row.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, float):
        # A fractional progress value is partial playback, which is explicitly
        # not proof of completion, so it stays unknown rather than truthy.
        if value in (0.0, 1.0):
            return value == 1.0
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "t", "yes", "y", "1", "completed", "watched"}:
            return True
        if normalized in {"false", "f", "no", "n", "0", "unwatched"}:
            return False
    return None


def _tracearr_row_completed(row: Mapping[str, object]) -> bool | None:
    """Return the completion flag for one Tracearr play chain."""
    for key in ("watched", "is_watched", "completed", "watched_status"):
        if key not in row:
            continue
        completed = _coerce_completed(row.get(key))
        if completed is not None:
            return completed
    return None


def _normalize_tracearr_events(
    rows: Iterable[Mapping[str, object]],
    usernames_by_identity: Mapping[str, str] | None,
    *,
    server_id: str,
    movie_min_seconds: int = PLAYBACK_MOVIE_MIN_SECONDS,
    episode_min_seconds: int = PLAYBACK_EPISODE_MIN_SECONDS,
) -> list[_NormalizedEvent]:
    """Convert Tracearr public-v2 play chains into normalized playback events."""

    events: list[_NormalizedEvent] = []
    for row in rows:
        if str(row.get("server_id") or "").strip() != server_id:
            continue
        media_type_raw = str(row.get("media_type") or "").strip().lower()
        if media_type_raw not in {"movie", "episode"}:
            continue
        media_type: Literal["movie", "episode"] = (
            "movie" if media_type_raw == "movie" else "episode"
        )
        duration_ms = _parse_int(row.get("duration_ms"))
        duration = max(0, duration_ms or 0) // 1000
        threshold = movie_min_seconds if media_type == "movie" else episode_min_seconds
        played_at = _parse_datetime(row.get("stopped_at") or row.get("started_at"))
        item_id = str(row.get("rating_key") or "").strip()
        event_id = str(row.get("id") or "").strip()
        raw_user = row.get("user")
        user: Mapping[str, object] = raw_user if isinstance(raw_user, Mapping) else {}
        user_id = str(user.get("id") or "").strip() or None
        username = (
            str(
                (
                    usernames_by_identity.get(user_id)
                    if user_id and usernames_by_identity
                    else ""
                )
                or user.get("username")
                or ""
            ).strip()
            or None
        )
        if not event_id or not item_id or played_at is None or duration < threshold:
            continue
        events.append(
            _NormalizedEvent(
                source_event_key=f"{server_id}:{event_id}",
                source_item_id=item_id,
                provider_media_type=media_type,
                played_at=played_at,
                duration_seconds=duration,
                source_user_id=user_id,
                source_username=username,
                completed=_tracearr_row_completed(row),
            )
        )
    return events


async def _identity_maps(
    session: AsyncSession,
    service: Service,
    config_id: int | None,
    *,
    ownership: MediaIdentityOwnership | None = None,
) -> tuple[
    dict[str, tuple[int, int]],
    dict[str, tuple[int, int, int, int, int, int]],
]:
    """Load local media ID maps for one media server's playback events.

    ``config_id`` names the media server the events were observed on. A Plex
    ratingKey (or Jellyfin/Emby item id) is only unique within one server, so a
    second server of the same type resolves through its own supplemental
    matches rather than the ids the main server wrote -- otherwise a play on
    one server lands on whichever media happens to share that id on the other.
    ``None`` means the events cannot name their server, which keeps the
    service-wide behaviour that predates multi-server support.

    Supplemental matches carry episode ids as well as movie ones, so a linked
    server of the main server's own type -- which may not write
    ``episodes.<service>_episode_id`` -- still resolves its episode plays.
    """

    if ownership is None:
        ownership = await load_media_identity_ownership(session)
    unattributed = config_id is None

    movie_map: dict[str, tuple[int, int]] = {}
    if unattributed or ownership.owns_media_rows(service, cast(int, config_id)):
        movie_map = {
            str(item_id): (movie_id, tmdb_id)
            for item_id, movie_id, tmdb_id in (
                await session.execute(
                    select(
                        MovieVersion.service_item_id,
                        Movie.id,
                        Movie.tmdb_id,
                    )
                    .join(Movie, MovieVersion.movie_id == Movie.id)
                    .where(
                        MovieVersion.service == service,
                        Movie.removed_at.is_(None),
                    )
                )
            ).all()
        }

    supplemental_query = (
        select(
            SupplementalMediaMatch.source_item_id,
            Movie.id,
            Movie.tmdb_id,
        )
        .join(Movie, SupplementalMediaMatch.movie_id == Movie.id)
        .where(
            SupplementalMediaMatch.media_type == MediaType.MOVIE,
            SupplementalMediaMatch.movie_id.is_not(None),
            Movie.removed_at.is_(None),
        )
    )
    supplemental_query = supplemental_query.where(
        SupplementalMediaMatch.source_service == service
        if unattributed
        else SupplementalMediaMatch.source_service_config_id == config_id
    )
    mapped_movies = (await session.execute(supplemental_query)).all()
    for item_id, movie_id, tmdb_id in mapped_movies:
        movie_map.setdefault(str(item_id), (movie_id, tmdb_id))

    episode_column = (
        {
            Service.PLEX: Episode.plex_rating_key,
            Service.JELLYFIN: Episode.jellyfin_episode_id,
            Service.EMBY: Episode.emby_episode_id,
        }.get(service)
        if unattributed
        or ownership.owns_service_id_columns(service, cast(int, config_id))
        else None
    )
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
                .where(
                    episode_column.is_not(None),
                    Series.removed_at.is_(None),
                )
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

    supplemental_episode_query = (
        select(
            SupplementalMediaMatch.source_item_id,
            Episode.id,
            Season.id,
            Series.id,
            Series.tmdb_id,
            Season.season_number,
            Episode.episode_number,
        )
        .join(Episode, SupplementalMediaMatch.episode_id == Episode.id)
        .join(Season, Episode.season_id == Season.id)
        .join(Series, Season.series_id == Series.id)
        .where(
            SupplementalMediaMatch.media_type == MediaType.SERIES,
            SupplementalMediaMatch.episode_id.is_not(None),
            Series.removed_at.is_(None),
        )
    )
    supplemental_episode_query = supplemental_episode_query.where(
        SupplementalMediaMatch.source_service == service
        if unattributed
        else SupplementalMediaMatch.source_service_config_id == config_id
    )
    for (
        item_id,
        episode_id,
        season_id,
        series_id,
        tmdb_id,
        season_number,
        episode_number,
    ) in (await session.execute(supplemental_episode_query)).all():
        episode_map.setdefault(
            str(item_id),
            (episode_id, season_id, series_id, tmdb_id, season_number, episode_number),
        )
    return movie_map, episode_map


async def _upsert_events(
    session: AsyncSession,
    *,
    config: ServiceConfig,
    provider: Service,
    observed_service: Service,
    observed_config_id: int | None,
    events: list[_NormalizedEvent],
) -> int:
    """Insert new playback events and update existing ones.

    ``observed_config_id`` is the media server the plays happened on, which is
    not the provider config whenever one provider fronts several servers (a
    Tracearr instance bound to two Plex servers). ``None`` when the provider
    cannot name it -- Tautulli on an install whose main server is not Plex.
    """

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

    movie_map, episode_map = await _identity_maps(
        session, observed_service, observed_config_id
    )
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
                    "observed_service": observed_service,
                    "observed_service_config_id": observed_config_id,
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
        existing.observed_service = observed_service
        existing.observed_service_config_id = observed_config_id
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
    source_event_prefix: str | None = None,
) -> None:
    """Apply current provider usernames to every retained event for a config."""

    if not usernames_by_id:
        return
    query = select(PlaybackHistoryEvent).where(
        PlaybackHistoryEvent.source_service_config_id == config_id,
        PlaybackHistoryEvent.source_user_id.is_not(None),
    )
    if source_event_prefix:
        query = query.where(
            PlaybackHistoryEvent.source_event_key.like(f"{source_event_prefix}%")
        )
    events = (await session.execute(query)).scalars().all()
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
    observed_config_id: int | None = None,
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
        observed_config_id=observed_config_id,
        available=False,
        error=error,
    )


async def _refresh_reporting_config(
    config: ServiceConfig,
    *,
    movie_min_seconds: int = PLAYBACK_MOVIE_MIN_SECONDS,
    episode_min_seconds: int = PLAYBACK_EPISODE_MIN_SECONDS,
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
                events = _normalize_reporting_events(
                    rows,
                    usernames_by_id,
                    movie_min_seconds=movie_min_seconds,
                    episode_min_seconds=episode_min_seconds,
                )
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
                observed_config_id=config.id,
            )

        if not plugin_available:
            return await _persist_unavailable_status(
                config,
                provider=config.service_type,
                observed_service=config.service_type,
                error=None,
                observed_config_id=config.id,
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
                # Playback Reporting reads the media server's own history, so
                # the provider config and the observed server are one row.
                observed_config_id=db_config.id,
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
            observed_config_id=config.id,
            available=True,
            imported_events=imported,
        )
    finally:
        if client is not None:
            await client.session.close()


async def _refresh_tautulli_config(
    config: ServiceConfig,
    *,
    movie_min_seconds: int = PLAYBACK_MOVIE_MIN_SECONDS,
    episode_min_seconds: int = PLAYBACK_EPISODE_MIN_SECONDS,
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
            events = _normalize_tautulli_events(
                rows,
                usernames_by_id,
                movie_min_seconds=movie_min_seconds,
                episode_min_seconds=episode_min_seconds,
            )
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
            # Tautulli's API cannot name which Plex it fronts, so attribute it
            # to the main server. Null when main is not Plex, which leaves its
            # events resolving service-wide the way they always have.
            observed_config_id = (
                await load_media_identity_ownership(session)
            ).main_config_id_for(Service.PLEX)
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
                observed_config_id=observed_config_id,
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
            observed_config_id=observed_config_id,
            available=True,
            imported_events=imported,
        )
    finally:
        if client is not None:
            await client.session.close()


def _tracearr_binding_state(
    config: ServiceConfig, binding: _TracearrBinding
) -> dict[str, object]:
    state = _state_from(config)
    bindings = state.get("bindings")
    if not isinstance(bindings, dict):
        return {}
    value = bindings.get(binding.tracearr_server_id)
    return dict(value) if isinstance(value, dict) else {}


def _recent_tracearr_binding_status(
    config: ServiceConfig, binding: _TracearrBinding
) -> PlaybackProviderStatus | None:
    state = _tracearr_binding_state(config, binding)
    if (
        _parse_int(state.get("format_version")) or 0
    ) < TRACEARR_PLAYBACK_FORMAT_VERSION:
        return None
    last_attempt = _parse_datetime(state.get("last_attempt_at"))
    if (
        last_attempt is None
        or datetime.now(UTC).replace(tzinfo=None) - last_attempt
        >= PLAYBACK_PREVIEW_REFRESH_INTERVAL
    ):
        return None
    return PlaybackProviderStatus(
        config_id=config.id,
        provider=Service.TRACEARR,
        observed_service=binding.observed_service,
        observed_config_id=binding.service_config_id,
        available=bool(state.get("available")),
        error=summarize_error_message(str(state.get("last_error") or "").strip())
        or None,
    )


def _set_tracearr_binding_state(
    config: ServiceConfig,
    binding: _TracearrBinding,
    *,
    available: bool,
    error: str | None,
    imported_events: int,
    last_event_at: datetime | None = None,
    unfinished_event_keys: Iterable[str] = (),
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    settings = (
        dict(config.extra_settings) if isinstance(config.extra_settings, dict) else {}
    )
    state = _state_from(config)
    raw_bindings = state.get("bindings")
    binding_states = dict(raw_bindings) if isinstance(raw_bindings, dict) else {}
    previous_raw = binding_states.get(binding.tracearr_server_id)
    previous = dict(previous_raw) if isinstance(previous_raw, dict) else {}
    binding_states[binding.tracearr_server_id] = {
        "available": available,
        "last_attempt_at": now.isoformat(),
        "last_success_at": (
            now.isoformat() if available else previous.get("last_success_at")
        ),
        "last_event_at": (
            last_event_at.isoformat()
            if last_event_at is not None
            else previous.get("last_event_at")
        ),
        "last_error": error,
        "imported_events": imported_events,
        "unfinished_event_keys": sorted(set(unfinished_event_keys)),
        "format_version": TRACEARR_PLAYBACK_FORMAT_VERSION,
        "observed_service": binding.observed_service.value,
        "service_config_id": binding.service_config_id,
    }
    state.update(
        {
            "provider": Service.TRACEARR.value,
            "bindings": binding_states,
            "aggregate_format_version": state.get("aggregate_format_version", 0),
        }
    )
    settings[PLAYBACK_HISTORY_STATE_KEY] = state
    config.extra_settings = settings


async def _fetch_tracearr_usernames(
    client: TracearrClient, server_id: str
) -> dict[str, str]:
    usernames: dict[str, str] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        rows, next_cursor = await client.get_users_page(cursor=cursor)
        for row in rows:
            identity_id = str(row.get("id") or "").strip()
            accounts = row.get("accounts")
            if not identity_id or not isinstance(accounts, list):
                continue
            removed_username: str | None = None
            for account in accounts:
                if not isinstance(account, Mapping):
                    continue
                if str(account.get("server_id") or "").strip() != server_id:
                    continue
                username = str(account.get("username") or "").strip()
                if not username:
                    continue
                if account.get("removed_at"):
                    # Keep it aside: retained history still carries this
                    # person's plays, and falling back to the opaque identity
                    # id would orphan them from requester matching.
                    removed_username = removed_username or username
                    continue
                usernames[identity_id] = username
                break
            else:
                if removed_username is not None:
                    usernames[identity_id] = removed_username
        if not next_cursor:
            return usernames
        if next_cursor in seen_cursors:
            raise ValueError("Tracearr users cursor repeated before completion")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


async def _persist_tracearr_unavailable_status(
    config: ServiceConfig,
    binding: _TracearrBinding,
    error: str,
) -> PlaybackProviderStatus:
    async with async_db() as session:
        db_config = await session.get(ServiceConfig, config.id)
        if db_config is not None:
            previous = _tracearr_binding_state(db_config, binding)
            unfinished = previous.get("unfinished_event_keys")
            _set_tracearr_binding_state(
                db_config,
                binding,
                available=False,
                error=error,
                imported_events=0,
                unfinished_event_keys=(
                    str(value)
                    for value in (unfinished if isinstance(unfinished, list) else [])
                ),
            )
            await session.commit()
    return PlaybackProviderStatus(
        config_id=config.id,
        provider=Service.TRACEARR,
        observed_service=binding.observed_service,
        observed_config_id=binding.service_config_id,
        available=False,
        error=error,
    )


async def _refresh_tracearr_binding(
    config: ServiceConfig,
    binding: _TracearrBinding,
    client: TracearrClient,
    *,
    movie_min_seconds: int,
    episode_min_seconds: int,
) -> PlaybackProviderStatus:
    try:
        usernames_by_identity = await _fetch_tracearr_usernames(
            client, binding.tracearr_server_id
        )
        state = _tracearr_binding_state(config, binding)
        unfinished_raw = state.get("unfinished_event_keys")
        unresolved_unfinished = {
            str(value)
            for value in (unfinished_raw if isinstance(unfinished_raw, list) else [])
            if str(value)
        }
        prefix = f"{binding.tracearr_server_id}:"
        async with async_db() as session:
            known_event_keys = set(
                (
                    await session.execute(
                        select(PlaybackHistoryEvent.source_event_key).where(
                            PlaybackHistoryEvent.source_service_config_id == config.id,
                            PlaybackHistoryEvent.source_service == Service.TRACEARR,
                            PlaybackHistoryEvent.source_event_key.like(f"{prefix}%"),
                        )
                    )
                ).scalars()
            )

        rows: list[Mapping[str, object]] = []
        current_unfinished: set[str] = set()
        reached_known_history = False
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page, next_cursor = await client.get_history_page(
                binding.tracearr_server_id, cursor=cursor
            )
            rows.extend(page)
            for row in page:
                row_server_id = str(row.get("server_id") or "").strip()
                row_server_type = str(row.get("server_type") or "").strip().lower()
                if row_server_id != binding.tracearr_server_id:
                    raise ValueError(
                        "Tracearr history returned a row for a different server"
                    )
                if row_server_type != binding.observed_service.value:
                    raise ValueError(
                        "Tracearr server type no longer matches its Reclaimerr binding"
                    )
                row_id = str(row.get("id") or "").strip()
                if not row_id:
                    continue
                event_key = f"{prefix}{row_id}"
                unresolved_unfinished.discard(event_key)
                if event_key in known_event_keys:
                    reached_known_history = True
                if row.get("stopped_at") is None:
                    current_unfinished.add(event_key)

            if not next_cursor or (reached_known_history and not unresolved_unfinished):
                if not next_cursor and unresolved_unfinished:
                    LOG.warning(
                        "Tracearr no longer returned "
                        f"{len(unresolved_unfinished)} unfinished play chain(s) for "
                        f"{binding.tracearr_server_name}; retaining their last snapshot"
                    )
                break
            if next_cursor in seen_cursors:
                raise ValueError("Tracearr history cursor repeated before completion")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        events = _normalize_tracearr_events(
            rows,
            usernames_by_identity,
            server_id=binding.tracearr_server_id,
            movie_min_seconds=movie_min_seconds,
            episode_min_seconds=episode_min_seconds,
        )
    except Exception as exc:
        error = _provider_error_message(exc)
        LOG.warning(
            f"Tracearr history refresh failed for {binding.tracearr_server_name}: {error}"
        )
        return await _persist_tracearr_unavailable_status(config, binding, error)

    if events and not any(event.completed for event in events):
        # A bound Tracearr server is the only completion signal for that media
        # server, so an all-unknown batch silently disables requester-watch
        # rules for it. Say so rather than reporting a healthy refresh.
        LOG.warning(
            f"Tracearr server {binding.tracearr_server_name} returned "
            f"{len(events)} playback event(s) but none marked watched; "
            "requester watch rules cannot use this source until it reports "
            "completion state"
        )

    async with async_db() as session:
        db_config = await session.get(ServiceConfig, config.id)
        if db_config is None:
            raise ValueError(f"Service config {config.id} no longer exists")
        await _backfill_event_usernames(
            session,
            config_id=db_config.id,
            usernames_by_id=usernames_by_identity,
            source_event_prefix=f"{binding.tracearr_server_id}:",
        )
        imported = await _upsert_events(
            session,
            config=db_config,
            provider=Service.TRACEARR,
            observed_service=binding.observed_service,
            observed_config_id=binding.service_config_id,
            events=events,
        )
        _set_tracearr_binding_state(
            db_config,
            binding,
            available=True,
            error=None,
            imported_events=imported,
            last_event_at=max((event.played_at for event in events), default=None),
            unfinished_event_keys=current_unfinished,
        )
        await session.commit()
    return PlaybackProviderStatus(
        config_id=config.id,
        provider=Service.TRACEARR,
        observed_service=binding.observed_service,
        observed_config_id=binding.service_config_id,
        available=True,
        imported_events=imported,
    )


async def _refresh_tracearr_config(
    config: ServiceConfig,
    *,
    bindings: list[_TracearrBinding] | None = None,
    force: bool,
    movie_min_seconds: int,
    episode_min_seconds: int,
) -> tuple[list[PlaybackProviderStatus], bool]:
    bindings = list(bindings) if bindings is not None else _tracearr_bindings(config)
    if not bindings:
        return [], False
    client = TracearrClient(
        api_key=_config_api_key(config),
        base_url=config.base_url,
        timeout=int((config.extra_settings or {}).get("timeout", 30)),
    )
    statuses: list[PlaybackProviderStatus] = []
    refreshed = False
    try:
        for binding in bindings:
            if not force:
                recent = _recent_tracearr_binding_status(config, binding)
                if recent is not None:
                    statuses.append(recent)
                    continue
            statuses.append(
                await _refresh_tracearr_binding(
                    config,
                    binding,
                    client,
                    movie_min_seconds=movie_min_seconds,
                    episode_min_seconds=episode_min_seconds,
                )
            )
            refreshed = True
    finally:
        await client.session.close()
    return statuses, refreshed


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
                            [
                                Service.JELLYFIN,
                                Service.EMBY,
                                Service.TAUTULLI,
                                Service.TRACEARR,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        media_configs = list(
            (
                await session.execute(
                    select(ServiceConfig).where(
                        ServiceConfig.enabled.is_(True),
                        ServiceConfig.service_type.in_(
                            [Service.PLEX, Service.JELLYFIN, Service.EMBY]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        general_settings = (
            (await session.execute(select(GeneralSettings))).scalars().first()
        )
        movie_min_seconds = (
            general_settings.playback_movie_min_seconds
            if general_settings is not None
            else PLAYBACK_MOVIE_MIN_SECONDS
        )
        episode_min_seconds = (
            general_settings.playback_episode_min_seconds
            if general_settings is not None
            else PLAYBACK_EPISODE_MIN_SECONDS
        )

    media_configs_by_id = {config.id: config for config in media_configs}
    tracearr_bindings_by_config: dict[int, list[_TracearrBinding]] = {}
    selected_tracearr_media_config_ids: set[int] = set()
    for config in configs:
        if config.service_type is not Service.TRACEARR:
            continue
        valid_bindings = [
            binding
            for binding in _tracearr_bindings(config)
            if (
                (media_config := media_configs_by_id.get(binding.service_config_id))
                is not None
                and media_config.service_type is binding.observed_service
            )
        ]
        tracearr_bindings_by_config[config.id] = valid_bindings
        selected_tracearr_media_config_ids.update(
            binding.service_config_id for binding in valid_bindings
        )

    selected_tracearr_services = {
        media_configs_by_id[config_id].service_type
        for config_id in selected_tracearr_media_config_ids
    }
    has_plex = any(config.service_type is Service.PLEX for config in media_configs)

    statuses: list[PlaybackProviderStatus] = []
    refreshed = False
    for config in configs:
        if (
            config.service_type in {Service.JELLYFIN, Service.EMBY}
            and config.id in selected_tracearr_media_config_ids
        ) or (
            config.service_type is Service.TAUTULLI
            and Service.PLEX in selected_tracearr_services
        ):
            continue
        if config.service_type is not Service.TRACEARR and not force:
            recent_status = _recent_status_from(config)
            if recent_status is not None:
                statuses.append(recent_status)
                continue
        if config.service_type in {Service.JELLYFIN, Service.EMBY}:
            if config.id in selected_tracearr_media_config_ids:
                continue
            statuses.append(
                await _refresh_reporting_config(
                    config,
                    movie_min_seconds=movie_min_seconds,
                    episode_min_seconds=episode_min_seconds,
                )
            )
            refreshed = True
        elif config.service_type == Service.TAUTULLI and has_plex:
            if Service.PLEX in selected_tracearr_services:
                continue
            statuses.append(
                await _refresh_tautulli_config(
                    config,
                    movie_min_seconds=movie_min_seconds,
                    episode_min_seconds=episode_min_seconds,
                )
            )
            refreshed = True
        elif config.service_type is Service.TRACEARR:
            tracearr_statuses, tracearr_refreshed = await _refresh_tracearr_config(
                config,
                bindings=tracearr_bindings_by_config.get(config.id, []),
                force=force,
                movie_min_seconds=movie_min_seconds,
                episode_min_seconds=episode_min_seconds,
            )
            statuses.extend(tracearr_statuses)
            refreshed = refreshed or tracearr_refreshed

    if force or refreshed:
        await resolve_unmapped_event_identities()
        await rebuild_playback_history_aggregates()
        await _mark_aggregate_format_current(config.id for config in configs)
    result = PlaybackRefreshResult(statuses=statuses)
    imported = sum(status.imported_events for status in statuses)
    LOG.info(
        "Playback history refresh complete: "
        f"{imported} new event(s), "
        f"{len(result.available_services)} observed service(s) available"
    )
    return result


async def resolve_unmapped_event_identities(*, batch_size: int = 5000) -> int:
    """Map retained events that had no local media match when they arrived.

    Providers are read incrementally, so an event imported before its media
    server IDs were synced -- or before a library rebuild changed them -- keeps
    a null TMDB ID forever and stays invisible to every rule that reads durable
    history. Re-resolving on each refresh recovers that history instead of
    requiring the provider to hand the same rows over again.
    """

    resolved = 0
    async with async_db() as session:
        ownership = await load_media_identity_ownership(session)
        # Grouped by observed server, not just service type - two servers of the
        # same type hand out the same item ids, so each needs its own map.
        sources = (
            await session.execute(
                select(
                    PlaybackHistoryEvent.observed_service,
                    PlaybackHistoryEvent.observed_service_config_id,
                )
                .where(PlaybackHistoryEvent.tmdb_id.is_(None))
                .distinct()
            )
        ).all()
        for service, observed_config_id in sources:
            if service is None:
                continue
            movie_map, episode_map = await _identity_maps(
                session, service, observed_config_id, ownership=ownership
            )
            if not movie_map and not episode_map:
                continue
            events = (
                (
                    await session.execute(
                        select(PlaybackHistoryEvent)
                        .where(
                            PlaybackHistoryEvent.tmdb_id.is_(None),
                            PlaybackHistoryEvent.observed_service == service,
                            PlaybackHistoryEvent.observed_service_config_id.is_(None)
                            if observed_config_id is None
                            else PlaybackHistoryEvent.observed_service_config_id
                            == observed_config_id,
                        )
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            for event in events:
                if event.provider_media_type == "movie":
                    identity = movie_map.get(event.source_item_id)
                    if identity is None:
                        continue
                    event.movie_id, event.tmdb_id = identity
                    resolved += 1
                    continue
                episode_identity = episode_map.get(event.source_item_id)
                if episode_identity is None:
                    continue
                (
                    event.episode_id,
                    event.season_id,
                    event.series_id,
                    event.tmdb_id,
                    event.season_number,
                    event.episode_number,
                ) = episode_identity
                resolved += 1
        if resolved:
            await session.commit()

    if resolved:
        LOG.info(f"Recovered local media identity for {resolved} playback event(s)")
    return resolved


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


def _observed_service_for_event(event: PlaybackHistoryEvent) -> Service:
    """Return the media server whose playback an imported event observes."""

    if event.observed_service is not None:
        return event.observed_service
    return (
        Service.PLEX
        if event.source_service is Service.TAUTULLI
        else event.source_service
    )


async def rebuild_playback_history_aggregates() -> None:
    """Remap retained events to current media rows and rebuild rule aggregates."""
    async with async_db() as session:
        active_tracearr_sources = await load_active_tracearr_sources(session)
        movie_rows = (
            await session.execute(
                select(Movie.id, Movie.tmdb_id, Movie.added_at).where(
                    Movie.removed_at.is_(None)
                )
            )
        ).all()
        movie_by_tmdb = {
            tmdb_id: (movie_id, added_at)
            for movie_id, tmdb_id, added_at in movie_rows
            if tmdb_id is not None
        }
        movie_added_by_id = {
            movie_id: added_at for movie_id, _tmdb_id, added_at in movie_rows
        }
        versions_by_movie: dict[int, list[int]] = defaultdict(list)
        for version_id, movie_id in (
            await session.execute(
                select(MovieVersion.id, MovieVersion.movie_id)
                .join(Movie, MovieVersion.movie_id == Movie.id)
                .where(
                    Movie.removed_at.is_(None),
                    MovieVersion.movie_id.is_not(None),
                )
            )
        ).all():
            if movie_id is None:
                continue
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
        series_added_by_id: dict[int, datetime | None] = {}
        season_added_by_id: dict[int, datetime | None] = {}
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
                .where(Series.removed_at.is_(None))
            )
        ).all():
            episode_by_identity[(tmdb_id, season_number, episode_number)] = (
                episode_id,
                season_id,
                series_id,
                series_added_at,
                season_added_at,
            )
            series_added_by_id[series_id] = series_added_at
            season_added_by_id[season_id] = season_added_at

        aggregates: dict[PlaybackTargetKey, _Aggregate] = {}
        user_aggregates: dict[
            tuple[int, Service, str, int],
            _UserAggregate,
        ] = {}
        movie_watch: dict[int, _Aggregate] = {}
        series_watch: dict[int, _Aggregate] = {}
        season_watch: dict[int, _Aggregate] = {}
        episode_watch: dict[int, _Aggregate] = {}
        remapped_events = 0
        unmatched_events = 0
        events = list(
            (await session.execute(select(PlaybackHistoryEvent))).scalars().all()
        )

        # One map per media server, not per service type: two servers of the
        # same type reuse each other's item ids, so a shared map would credit a
        # play to whichever media happened to hold that id on the other server.
        ownership = await load_media_identity_ownership(session)
        identity_maps: dict[
            tuple[Service, int | None],
            tuple[
                dict[str, tuple[int, int]],
                dict[str, tuple[int, int, int, int, int, int]],
            ],
        ] = {}
        for identity_key in {
            (_observed_service_for_event(event), event.observed_service_config_id)
            for event in events
        }:
            identity_maps[identity_key] = await _identity_maps(
                session,
                identity_key[0],
                identity_key[1],
                ownership=ownership,
            )

        for event in events:
            if not _playback_event_is_active(event, active_tracearr_sources):
                continue
            target_keys: list[PlaybackTargetKey] = []
            previous_ids = (
                event.movie_id,
                event.series_id,
                event.season_id,
                event.episode_id,
            )
            observed_service = _observed_service_for_event(event)
            movie_map, episode_map = identity_maps.get(
                (observed_service, event.observed_service_config_id), ({}, {})
            )

            if event.provider_media_type == "movie":
                movie_id: int | None = None
                movie_added_at: datetime | None = None
                direct_movie = movie_map.get(event.source_item_id)
                if direct_movie is not None:
                    movie_id, tmdb_id = direct_movie
                    event.tmdb_id = tmdb_id
                    movie_added_at = movie_added_by_id.get(movie_id)
                elif event.tmdb_id is not None:
                    movie_identity = movie_by_tmdb.get(event.tmdb_id)
                    if movie_identity is not None:
                        movie_id, movie_added_at = movie_identity

                event.movie_id = movie_id
                event.series_id = event.season_id = event.episode_id = None
                if movie_id is not None:
                    target_keys.extend(
                        ("movie_version", version_id)
                        for version_id in versions_by_movie.get(movie_id, [])
                    )
                    if movie_added_at is None or event.played_at >= movie_added_at:
                        movie_watch.setdefault(
                            movie_id, _Aggregate(media_type=MediaType.MOVIE)
                        ).add(event)
            elif event.provider_media_type == "episode":
                episode_id = season_id = series_id = None
                series_added_at = season_added_at = None
                direct_episode = episode_map.get(event.source_item_id)
                if direct_episode is not None:
                    (
                        episode_id,
                        season_id,
                        series_id,
                        tmdb_id,
                        season_number,
                        episode_number,
                    ) = direct_episode
                    event.tmdb_id = tmdb_id
                    event.season_number = season_number
                    event.episode_number = episode_number
                    series_added_at = series_added_by_id.get(series_id)
                    season_added_at = season_added_by_id.get(season_id)
                elif (
                    event.tmdb_id is not None
                    and event.season_number is not None
                    and event.episode_number is not None
                ):
                    identity = episode_by_identity.get(
                        (event.tmdb_id, event.season_number, event.episode_number)
                    )
                    if identity is not None:
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
                if (
                    episode_id is not None
                    and season_id is not None
                    and series_id is not None
                ):
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

            current_ids = (
                event.movie_id,
                event.series_id,
                event.season_id,
                event.episode_id,
            )
            if target_keys and current_ids != previous_ids:
                remapped_events += 1
            if not target_keys:
                unmatched_events += 1

            for key in target_keys:
                media_type = (
                    MediaType.MOVIE if key[0] == "movie_version" else MediaType.SERIES
                )
                aggregates.setdefault(
                    key,
                    _Aggregate(media_type=media_type),
                ).add(event)
                user_aggregates.setdefault(
                    (
                        event.source_service_config_id,
                        observed_service,
                        key[0],
                        key[1],
                    ),
                    _UserAggregate(media_type=media_type),
                ).add(event)

        await session.execute(delete(PlaybackHistoryAggregate))
        await session.execute(delete(PlaybackHistoryUserAggregate))
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
        session.add_all(
            [
                PlaybackHistoryUserAggregate(
                    source_service_config_id=config_id,
                    observed_service=observed_service,
                    target_scope=scope,
                    target_id=target_id,
                    media_type=aggregate.media_type,
                    unique_user_count=len(aggregate.users),
                    usernames=sorted(
                        aggregate.usernames.values(),
                        key=str.casefold,
                    ),
                    usernames_complete=aggregate.usernames_complete,
                )
                for (
                    config_id,
                    observed_service,
                    scope,
                    target_id,
                ), aggregate in user_aggregates.items()
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
        LOG.info(
            "Playback aggregate rebuild complete: "
            f"{len(aggregates)} target aggregate(s), "
            f"{len(user_aggregates)} source-user aggregate(s), "
            f"{remapped_events} retained event(s) remapped"
        )
        if unmatched_events:
            LOG.debug(
                f"Playback aggregate rebuild retained {unmatched_events} "
                "event(s) without a current media target"
            )


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
                SupplementalMediaMatch.episode_id,
            )
        )
    ).all()
    supplemental_movie_ids: dict[Service, set[int]] = defaultdict(set)
    for service, movie_id, series_id, season_id, episode_id in supplemental_rows:
        if movie_id is not None:
            supplemental_movie_ids[service].add(movie_id)
        if series_id is not None:
            target_services["series"].setdefault(series_id, set()).add(service)
        if season_id is not None:
            target_services["season"].setdefault(season_id, set()).add(service)
        if episode_id is not None:
            target_services["episode"].setdefault(episode_id, set()).add(service)
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
    legacy_plugin_users_by_target: dict[PlaybackTargetKey, _UserEvidence] = {}
    for row in plugin_rows:
        legacy_plugin_users_by_target[(row.target_scope, row.target_id)] = (
            _UserEvidence(
                unique_user_count=row.unique_user_count,
                usernames={
                    str(name).casefold(): str(name)
                    for name in row.usernames or []
                    if str(name or "").strip()
                },
                usernames_complete=row.usernames_complete,
            )
        )

    source_user_rows = (
        (await db.execute(select(PlaybackHistoryUserAggregate))).scalars().all()
    )
    plugin_users_by_source: dict[
        tuple[PlaybackTargetKey, Service],
        _UserEvidence,
    ] = {}
    for row in source_user_rows:
        evidence = plugin_users_by_source.setdefault(
            (
                (row.target_scope, row.target_id),
                row.observed_service,
            ),
            _UserEvidence(),
        )
        evidence.merge(
            unique_user_count=row.unique_user_count,
            usernames=row.usernames or [],
            usernames_complete=row.usernames_complete,
        )

    incomplete_timestamp_targets: set[PlaybackTargetKey] = set()
    native_values_by_target: dict[PlaybackTargetKey, dict[str, object]] = {}
    native_users_by_source: dict[
        tuple[PlaybackTargetKey, Service],
        _UserEvidence,
    ] = {}
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
            if row.has_activity and row.last_activity_at is None:
                incomplete_timestamp_targets.add(key)
            native_values_by_target[key] = _merge_playback_source_values(
                native_values_by_target.get(key),
                _native_aggregate_values(row),
            )
            native_user_evidence = native_users_by_source.setdefault(
                (key, row.source_service),
                _UserEvidence(),
            )
            native_user_evidence.merge(
                unique_user_count=row.unique_user_count,
                usernames=row.usernames or [],
                usernames_complete=row.usernames_complete,
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
        available_fields_by_target.setdefault(key, set()).update(
            field for field in values if field not in PLAYBACK_USER_FIELDS
        )
    for key in plugin_covered_targets:
        if key not in plugin_values_by_target:
            plugin_values_by_target[key] = dict(plugin_zero_values)
            available_fields_by_target.setdefault(key, set()).update(
                field
                for field in plugin_zero_values
                if field not in PLAYBACK_USER_FIELDS
            )
    for key, values in native_values_by_target.items():
        available_fields_by_target.setdefault(key, set()).update(
            field
            for field in values
            if field in NATIVE_PLAYBACK_FIELDS and field not in PLAYBACK_USER_FIELDS
        )
    for key in native_covered_targets:
        if key not in native_values_by_target:
            native_values_by_target[key] = dict(native_zero_values)
            available_fields_by_target.setdefault(key, set()).update(
                field
                for field in native_zero_values
                if field not in PLAYBACK_USER_FIELDS
            )

    values_by_target: dict[PlaybackTargetKey, dict[str, object]] = {}
    for key in plugin_values_by_target.keys() | native_values_by_target.keys():
        values_by_target[key] = _merge_playback_metric_values(
            plugin_values_by_target.get(key), native_values_by_target.get(key)
        )

    native_statuses_by_service: dict[Service, list[NativePlaybackStatus]] = defaultdict(
        list
    )
    for status in native_statuses:
        native_statuses_by_service[status.service].append(status)

    # User authority is service-scoped. Plex uses retained Tautulli identities,
    # while Jellyfin and Emby use their current native watched state. A target
    # available on multiple servers combines those authoritative sources
    # instead of allowing one server to replace every other server's users.
    for scope, services_by_target in target_services.items():
        for target_id, services in services_by_target.items():
            key = (scope, target_id)
            combined = _UserEvidence()
            has_authoritative_source = False
            source_unknown = False
            for service in services:
                if service is Service.PLEX:
                    evidence = plugin_users_by_source.get((key, service))
                    if evidence is None and len(services) == 1:
                        evidence = legacy_plugin_users_by_target.get(key)
                    if evidence is not None:
                        combined.merge(
                            unique_user_count=evidence.unique_user_count,
                            usernames=evidence.usernames.values(),
                            usernames_complete=evidence.usernames_complete,
                        )
                        has_authoritative_source = True
                    elif service in plugin_available_services:
                        has_authoritative_source = True
                    else:
                        source_unknown = True
                    continue

                if service not in {Service.JELLYFIN, Service.EMBY}:
                    continue
                statuses = native_statuses_by_service.get(service, [])
                if not any(status.available for status in statuses):
                    source_unknown = True
                    continue
                evidence = native_users_by_source.get((key, service))
                if evidence is not None:
                    combined.merge(
                        unique_user_count=evidence.unique_user_count,
                        usernames=evidence.usernames.values(),
                        usernames_complete=evidence.usernames_complete,
                    )
                has_authoritative_source = True

            available_fields = available_fields_by_target.setdefault(key, set())
            existing_values = values_by_target.get(key)
            if existing_values is not None:
                existing_values.pop("playback.usernames", None)
                existing_values.pop("playback.unique_user_count", None)
            available_fields.discard("playback.usernames")
            available_fields.discard("playback.unique_user_count")
            if source_unknown or not has_authoritative_source:
                continue
            user_values = combined.values()
            values = values_by_target.setdefault(key, {})
            values.update(user_values)
            available_fields.update(user_values)

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


async def load_user_playback_totals(
    db: AsyncSession,
) -> dict[PlaybackTargetKey, dict[str, dict[str, object]]]:
    """Return per-(target, user) playback totals for user-scoped rule conditions.

    Computed on demand from durable ``PlaybackHistoryEvent`` rows -- there is no
    persisted per-user rollup table, mirroring how ``load_playback_rule_snapshot``
    loads the whole ``PlaybackHistoryAggregate`` table unconditionally rather than
    filtering to a specific scan batch. Movie-version targets are fanned out from
    their parent movie's totals, since playback events are recorded per movie, not
    per physical file version (a movie's multiple versions share one playback
    history).

    Returns a mapping of ``(target_scope, target_id)`` to
    ``{normalized_username: {"display_username", "total_duration_seconds",
    "play_count", "last_activity_at"}}``.
    """
    totals: dict[PlaybackTargetKey, dict[str, dict[str, object]]] = {}
    active_tracearr_sources = await load_active_tracearr_sources(db)
    active_event_clause = active_playback_event_clause(active_tracearr_sources)

    def _merge(
        scope: str,
        target_id: int,
        username: str,
        total_seconds: int,
        play_count: int,
        last_activity_at: datetime | None,
    ) -> None:
        display_username = username.strip()
        normalized = display_username.casefold()
        if not normalized:
            return
        bucket = totals.setdefault((scope, target_id), {})
        existing = bucket.get(normalized)
        if existing is None:
            bucket[normalized] = {
                "display_username": display_username,
                "total_duration_seconds": total_seconds,
                "play_count": play_count,
                "last_activity_at": last_activity_at,
            }
            return
        existing["total_duration_seconds"] = (
            cast(int, existing["total_duration_seconds"]) + total_seconds
        )
        existing["play_count"] = cast(int, existing["play_count"]) + play_count
        existing_last = cast("datetime | None", existing["last_activity_at"])
        if last_activity_at is not None and (
            existing_last is None or last_activity_at > existing_last
        ):
            existing["last_activity_at"] = last_activity_at

    # movies: playback events are grouped by movie_id, then fanned out to every
    # active version of that movie (rule targets are movie versions, not movies)
    movie_rows = (
        await db.execute(
            select(
                PlaybackHistoryEvent.movie_id,
                PlaybackHistoryEvent.source_username,
                func.sum(PlaybackHistoryEvent.duration_seconds),
                func.count(PlaybackHistoryEvent.id),
                func.max(PlaybackHistoryEvent.played_at),
            )
            .where(
                PlaybackHistoryEvent.movie_id.is_not(None),
                PlaybackHistoryEvent.source_username.is_not(None),
                active_event_clause,
            )
            .group_by(
                PlaybackHistoryEvent.movie_id, PlaybackHistoryEvent.source_username
            )
        )
    ).all()
    if movie_rows:
        version_ids_by_movie: dict[int, list[int]] = defaultdict(list)
        for version_id, movie_id in (
            await db.execute(
                select(MovieVersion.id, MovieVersion.movie_id)
                .join(Movie, MovieVersion.movie_id == Movie.id)
                .where(Movie.removed_at.is_(None), MovieVersion.movie_id.is_not(None))
            )
        ).all():
            version_ids_by_movie[movie_id].append(version_id)
        for (
            movie_id,
            username,
            total_seconds,
            play_count,
            last_activity_at,
        ) in movie_rows:
            if not username:
                continue
            for version_id in version_ids_by_movie.get(movie_id, []):
                _merge(
                    "movie_version",
                    version_id,
                    username,
                    int(total_seconds or 0),
                    int(play_count or 0),
                    last_activity_at,
                )

    # series/season/episode: the FK id already matches the rule target id directly
    for scope, column in (
        ("series", PlaybackHistoryEvent.series_id),
        ("season", PlaybackHistoryEvent.season_id),
        ("episode", PlaybackHistoryEvent.episode_id),
    ):
        rows = (
            await db.execute(
                select(
                    column,
                    PlaybackHistoryEvent.source_username,
                    func.sum(PlaybackHistoryEvent.duration_seconds),
                    func.count(PlaybackHistoryEvent.id),
                    func.max(PlaybackHistoryEvent.played_at),
                )
                .where(
                    column.is_not(None),
                    PlaybackHistoryEvent.source_username.is_not(None),
                    active_event_clause,
                )
                .group_by(column, PlaybackHistoryEvent.source_username)
            )
        ).all()
        for target_id, username, total_seconds, play_count, last_activity_at in rows:
            if not username:
                continue
            _merge(
                scope,
                target_id,
                username,
                int(total_seconds or 0),
                int(play_count or 0),
                last_activity_at,
            )

    return totals
