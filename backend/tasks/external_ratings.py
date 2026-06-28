from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from cryptography.fernet import InvalidToken
from sqlalchemy import or_, select

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.core.task_tracking import track_task_execution
from backend.core.utils.datetime_utils import ensure_utc
from backend.database import async_db
from backend.database.models import (
    ExternalRatingsIngestState,
    Movie,
    Series,
    ServiceConfig,
)
from backend.enums import MediaType, Service, Task
from backend.services.external_ratings import (
    DEFAULT_MDBLIST_BASE_URL,
    DEFAULT_OMDB_BASE_URL,
    EXTERNAL_RATING_FIELDS,
    ExternalRatingValues,
    MDBListClient,
    OMDbClient,
    ProviderRateLimitError,
    ProviderRateLimitSnapshot,
)

ERROR_MSG_MAX_LENGTH = 2000
DEFAULT_STALE_AFTER_DAYS = 14
DEFAULT_MDBLIST_REQUEST_LIMIT = 950
DEFAULT_OMDB_REQUEST_LIMIT = 950
DEFAULT_MDBLIST_REQUEST_DELAY_SECONDS = 1.0
MDBLIST_SUPPORTER_REQUEST_DELAY_SECONDS = 0.2
DEFAULT_OMDB_REQUEST_DELAY_SECONDS = 0.25

SUPPORTED_PROVIDERS = (Service.MDBLIST, Service.OMDB)

__all__ = [
    "refresh_mdblist_ratings",
    "refresh_omdb_ratings",
]


@dataclass(slots=True)
class _ProviderState:
    config: ServiceConfig
    api_key: str
    request_limit: int
    request_delay_seconds: float
    requests_used: int = 0
    last_request_at: datetime | None = None
    disabled_reason: str | None = None
    rate_limit: ProviderRateLimitSnapshot | None = None

    @property
    def available(self) -> bool:
        return (
            self.disabled_reason is None
            and self.requests_used < self.request_limit
            and (self.rate_limit is None or self.rate_limit.remaining != 0)
        )

    def update_rate_limit(self, snapshot: ProviderRateLimitSnapshot | None) -> None:
        if snapshot is not None:
            self.rate_limit = snapshot

    async def wait_for_next_request(self) -> None:
        if self.request_delay_seconds <= 0 or self.last_request_at is None:
            return
        elapsed = (datetime.now(UTC) - self.last_request_at).total_seconds()
        delay = self.request_delay_seconds - elapsed
        if delay > 0:
            await asyncio.sleep(delay)

    def mark_request_attempted(self) -> None:
        self.last_request_at = datetime.now(UTC)


@dataclass(slots=True)
class _MediaItem:
    media_type: MediaType
    row_id: int
    provider_id: int | str
    refreshed_at: datetime | None


@dataclass(slots=True)
class _ProviderResult:
    item: _MediaItem
    values: ExternalRatingValues


async def refresh_mdblist_ratings() -> None:
    """Refresh cached ratings supplied by MDBList."""
    async with track_task_execution(Task.MDBLIST_RATINGS_REFRESH):
        await _refresh_provider(Service.MDBLIST)


async def refresh_omdb_ratings() -> None:
    """Refresh OMDb values used when MDBList has rating gaps."""
    async with track_task_execution(Task.OMDB_RATINGS_REFRESH):
        await _refresh_provider(Service.OMDB)


async def _refresh_provider(provider: Service) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported external ratings provider: {provider}")

    state = await _get_or_create_state()
    checked_at = datetime.now(UTC)
    try:
        provider_state = await _load_provider_state(provider)
        if provider_state is None:
            LOG.info(
                f"Skipping {provider.value} ratings refresh: provider is not enabled"
            )
            await _persist_success(
                state.id,
                provider,
                checked_at,
                provider_state=None,
                movie_count=0,
                series_count=0,
                updated_count=0,
            )
            return

        items = await _load_stale_items(
            provider,
            now=checked_at,
            limit=provider_state.request_limit,
        )
        if not items:
            LOG.info(f"{provider.value} ratings refresh skipped: no stale media rows")
            await _persist_success(
                state.id,
                provider,
                checked_at,
                provider_state=provider_state,
                movie_count=0,
                series_count=0,
                updated_count=0,
            )
            return

        results: list[_ProviderResult] = []
        movie_count = 0
        series_count = 0
        client = _provider_client(provider_state)
        for item in items:
            if not provider_state.available:
                break
            values = await _fetch_provider_values(
                provider,
                provider_state,
                client,
                item,
            )
            if values is None:
                continue
            results.append(_ProviderResult(item=item, values=values))
            if item.media_type is MediaType.MOVIE:
                movie_count += 1
            else:
                series_count += 1

        enabled_providers = await _load_enabled_providers()
        updated_count = await _persist_provider_results(
            provider,
            results,
            refreshed_at=checked_at,
            enabled_providers=enabled_providers,
        )
        await _persist_success(
            state.id,
            provider,
            checked_at,
            provider_state=provider_state,
            movie_count=movie_count,
            series_count=series_count,
            updated_count=updated_count,
        )
        LOG.info(
            f"{provider.value} ratings refresh complete: {updated_count} updated, "
            f"{provider_state.requests_used} provider request(s)"
        )
    except Exception as exc:
        await _persist_error(state.id, provider, checked_at, str(exc))
        raise


def _provider_client(provider: _ProviderState) -> MDBListClient | OMDbClient:
    if provider.config.service_type is Service.MDBLIST:
        return MDBListClient(
            api_key=provider.api_key,
            base_url=provider.config.base_url or DEFAULT_MDBLIST_BASE_URL,
        )
    return OMDbClient(
        api_key=provider.api_key,
        base_url=provider.config.base_url or DEFAULT_OMDB_BASE_URL,
    )


async def _fetch_provider_values(
    provider: Service,
    state: _ProviderState,
    client: MDBListClient | OMDbClient,
    item: _MediaItem,
) -> ExternalRatingValues | None:
    try:
        await state.wait_for_next_request()
        state.mark_request_attempted()
        if provider is Service.MDBLIST:
            assert isinstance(client, MDBListClient)
            tmdb_id = item.provider_id
            if not isinstance(tmdb_id, int):
                return None
            values = await client.get_ratings(item.media_type, tmdb_id)
        else:
            assert isinstance(client, OMDbClient)
            imdb_id = item.provider_id
            if not isinstance(imdb_id, str):
                return None
            values = await client.get_ratings(imdb_id)
        state.update_rate_limit(client.last_rate_limit)
        state.requests_used += 1
        return values
    except ProviderRateLimitError as exc:
        state.update_rate_limit(exc.rate_limit)
        state.disabled_reason = str(exc)
        LOG.warning(str(exc))
    except Exception as exc:
        state.update_rate_limit(client.last_rate_limit)
        state.requests_used += 1
        LOG.debug(
            f"{provider.value} rating lookup failed for "
            f"{item.media_type.value} row {item.row_id}: {exc}"
        )
    return None


async def _persist_provider_results(
    provider: Service,
    results: list[_ProviderResult],
    *,
    refreshed_at: datetime,
    enabled_providers: set[Service],
) -> int:
    updated_count = 0
    async with async_db() as db:
        for result in results:
            row: Movie | Series | None
            if result.item.media_type is MediaType.MOVIE:
                row = await db.get(Movie, result.item.row_id)
            else:
                row = await db.get(Series, result.item.row_id)
            if row is None:
                continue
            if provider is Service.MDBLIST:
                row.mdblist_ratings_cache = result.values.to_dict()
                row.mdblist_ratings_refreshed_at = refreshed_at
            else:
                row.omdb_ratings_cache = result.values.to_dict()
                row.omdb_ratings_refreshed_at = refreshed_at
            if _materialize_effective_ratings(row, enabled_providers):
                updated_count += 1
        await db.commit()
    return updated_count


def _materialize_effective_ratings(
    row: Movie | Series,
    enabled_providers: set[Service],
) -> bool:
    previous = {field: getattr(row, field) for field in EXTERNAL_RATING_FIELDS}
    previous_source = row.external_ratings_source

    mdblist = ExternalRatingValues.from_mapping(row.mdblist_ratings_cache)
    omdb = ExternalRatingValues.from_mapping(row.omdb_ratings_cache)
    effective = ExternalRatingValues()
    sources: list[str] = []

    if row.mdblist_ratings_cache is not None:
        effective.merge_missing(mdblist)
        if mdblist.has_any():
            sources.append(Service.MDBLIST.value)
    if row.omdb_ratings_cache is not None:
        before = effective.to_dict()
        effective.merge_missing(omdb)
        if any(
            before[field] is None and getattr(omdb, field) is not None
            for field in EXTERNAL_RATING_FIELDS
        ):
            sources.append(Service.OMDB.value)

    caches_initialized = (
        row.mdblist_ratings_cache is not None
        or Service.MDBLIST not in enabled_providers
    ) and (row.omdb_ratings_cache is not None or Service.OMDB not in enabled_providers)
    if not caches_initialized:
        for field in EXTERNAL_RATING_FIELDS:
            if getattr(effective, field) is None and previous[field] is not None:
                setattr(effective, field, previous[field])
        for source in str(previous_source or "").split("+"):
            if (
                source in {Service.MDBLIST.value, Service.OMDB.value}
                and source not in sources
            ):
                sources.append(source)

    for field in EXTERNAL_RATING_FIELDS:
        setattr(row, field, getattr(effective, field))
    source_set = set(sources)
    row.external_ratings_source = (
        "+".join(
            source.value for source in SUPPORTED_PROVIDERS if source.value in source_set
        )
        if source_set
        else "none"
    )
    timestamps = [
        value
        for value in (
            row.mdblist_ratings_refreshed_at,
            row.omdb_ratings_refreshed_at,
        )
        if value is not None
    ]
    row.external_ratings_refreshed_at = (
        max(timestamps, key=ensure_utc) if timestamps else None
    )

    return previous != {
        field: getattr(row, field) for field in EXTERNAL_RATING_FIELDS
    } or (previous_source != row.external_ratings_source)


async def _load_stale_items(
    provider: Service,
    *,
    now: datetime,
    limit: int,
) -> list[_MediaItem]:
    stale_before = now - timedelta(days=DEFAULT_STALE_AFTER_DAYS)
    timestamp_name: Literal[
        "mdblist_ratings_refreshed_at", "omdb_ratings_refreshed_at"
    ] = (
        "mdblist_ratings_refreshed_at"
        if provider is Service.MDBLIST
        else "omdb_ratings_refreshed_at"
    )
    identifier_name = "tmdb_id" if provider is Service.MDBLIST else "imdb_id"

    async with async_db() as db:
        items: list[_MediaItem] = []
        for media_type, model in (
            (MediaType.MOVIE, Movie),
            (MediaType.SERIES, Series),
        ):
            timestamp = getattr(model, timestamp_name)
            identifier = getattr(model, identifier_name)
            filters = [
                identifier.isnot(None),
                or_(timestamp.is_(None), timestamp < stale_before),
            ]
            if provider is Service.OMDB:
                filters.append(
                    or_(
                        model.external_ratings_source.like("%omdb%"),
                        model.rottentomatoes_tomato_meter.is_(None),
                        model.metacritic_metascore.is_(None),
                    )
                )
            rows = (
                await db.execute(
                    select(model.id, identifier, timestamp)
                    .where(*filters)
                    .order_by(timestamp.isnot(None), timestamp.asc(), model.id.asc())
                    .limit(limit)
                )
            ).all()
            items.extend(
                _MediaItem(media_type, row_id, provider_id, refreshed_at)
                for row_id, provider_id, refreshed_at in rows
                if isinstance(provider_id, (int, str))
            )

    items.sort(
        key=lambda item: (
            item.refreshed_at is not None,
            ensure_utc(item.refreshed_at)
            if item.refreshed_at is not None
            else datetime.min.replace(tzinfo=UTC),
            item.media_type.value,
            item.row_id,
        )
    )
    return items[:limit]


async def _load_provider_state(provider: Service) -> _ProviderState | None:
    async with async_db() as db:
        config = (
            (
                await db.execute(
                    select(ServiceConfig)
                    .where(
                        ServiceConfig.enabled.is_(True),
                        ServiceConfig.service_type == provider,
                    )
                    .order_by(ServiceConfig.id.asc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if config is None:
            return None
        default_limit = (
            DEFAULT_MDBLIST_REQUEST_LIMIT
            if provider is Service.MDBLIST
            else DEFAULT_OMDB_REQUEST_LIMIT
        )
        return _ProviderState(
            config=config,
            api_key=_config_api_key(config),
            request_limit=_request_limit(config, default_limit),
            request_delay_seconds=_request_delay_seconds(config),
        )


async def _load_enabled_providers() -> set[Service]:
    async with async_db() as db:
        return set(
            (
                await db.execute(
                    select(ServiceConfig.service_type).where(
                        ServiceConfig.enabled.is_(True),
                        ServiceConfig.service_type.in_(SUPPORTED_PROVIDERS),
                    )
                )
            ).scalars()
        )


def _config_api_key(config: ServiceConfig) -> str:
    try:
        return fer_decrypt(config.api_key)
    except InvalidToken:
        if config.api_key.startswith("gAAAA"):
            raise
        return config.api_key


def _request_limit(config: ServiceConfig, default: int) -> int:
    raw = (config.extra_settings or {}).get("request_limit", default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 5000))


def _request_delay_seconds(config: ServiceConfig) -> float:
    default = (
        _default_mdblist_request_delay(config.extra_settings or {})
        if config.service_type is Service.MDBLIST
        else DEFAULT_OMDB_REQUEST_DELAY_SECONDS
    )
    raw = (config.extra_settings or {}).get("request_delay_seconds", default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(value, 10.0))


def _default_mdblist_request_delay(extra_settings: dict[str, Any]) -> float:
    if bool(extra_settings.get("supporter_mode")):
        return MDBLIST_SUPPORTER_REQUEST_DELAY_SECONDS
    return DEFAULT_MDBLIST_REQUEST_DELAY_SECONDS


def _provider_summary(
    provider: Service,
    state: _ProviderState | None,
    *,
    checked_at: datetime,
    successful: bool,
    error: str | None = None,
) -> dict[str, Any]:
    default_limit = (
        DEFAULT_MDBLIST_REQUEST_LIMIT
        if provider is Service.MDBLIST
        else DEFAULT_OMDB_REQUEST_LIMIT
    )
    return {
        "requests_used": state.requests_used if state is not None else 0,
        "request_limit": state.request_limit if state is not None else default_limit,
        "request_delay_seconds": state.request_delay_seconds
        if state is not None
        else None,
        "disabled_reason": state.disabled_reason if state is not None else None,
        "rate_limit": (
            state.rate_limit.to_dict()
            if state is not None and state.rate_limit is not None
            else None
        ),
        "last_checked_at": checked_at.isoformat(),
        "last_successful_refresh_at": checked_at.isoformat() if successful else None,
        "last_error": error,
    }


async def _get_or_create_state() -> ExternalRatingsIngestState:
    async with async_db() as db:
        row = (await db.execute(select(ExternalRatingsIngestState))).scalars().first()
        if row is None:
            row = ExternalRatingsIngestState()
            db.add(row)
            await db.commit()
        return row


async def _persist_success(
    state_id: int,
    provider: Service,
    checked_at: datetime,
    *,
    provider_state: _ProviderState | None,
    movie_count: int,
    series_count: int,
    updated_count: int,
) -> None:
    async with async_db() as db:
        state = await db.get(ExternalRatingsIngestState, state_id)
        if state is None:
            return
        summaries = dict(state.provider_summary or {})
        summaries[provider.value] = _provider_summary(
            provider,
            provider_state,
            checked_at=checked_at,
            successful=True,
        )
        state.provider_summary = summaries
        state.movie_count = movie_count
        state.series_count = series_count
        state.request_count = provider_state.requests_used if provider_state else 0
        state.updated_count = updated_count
        state.last_checked_at = checked_at
        state.last_successful_refresh_at = checked_at
        state.last_error = None
        await db.commit()


async def _persist_error(
    state_id: int,
    provider: Service,
    checked_at: datetime,
    error: str,
) -> None:
    async with async_db() as db:
        state = await db.get(ExternalRatingsIngestState, state_id)
        if state is None:
            return
        message = (error or "unknown error")[:ERROR_MSG_MAX_LENGTH]
        summaries = dict(state.provider_summary or {})
        previous = summaries.get(provider.value)
        provider_state = dict(previous) if isinstance(previous, dict) else {}
        provider_state["last_checked_at"] = checked_at.isoformat()
        provider_state["last_error"] = message
        summaries[provider.value] = provider_state
        state.provider_summary = summaries
        state.last_checked_at = checked_at
        state.last_error = message
        await db.commit()
