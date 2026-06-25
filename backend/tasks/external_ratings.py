from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import or_, select

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.core.task_tracking import track_task_execution
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

__all__ = ["refresh_external_ratings"]


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
    row: Movie | Series


async def refresh_external_ratings() -> None:
    """Refresh external rating scores from configured providers."""
    async with track_task_execution(Task.REFRESH_EXTERNAL_RATINGS):
        state = await _get_or_create_state()
        now = datetime.now(UTC)

        try:
            provider_states = await _load_provider_states()
            mdblist = provider_states.get(Service.MDBLIST)
            omdb = provider_states.get(Service.OMDB)
            if not mdblist and not omdb:
                LOG.info("Skipping external ratings refresh: no enabled providers")
                await _persist_success(
                    state.id,
                    now,
                    provider_summary={},
                    movie_count=0,
                    series_count=0,
                    request_count=0,
                    updated_count=0,
                )
                return

            max_items = max(
                mdblist.request_limit if mdblist else 0,
                omdb.request_limit if omdb else 0,
                1,
            )
            async with async_db() as db:
                items = await _load_stale_items(db, now=now, limit=max_items)
                if not items:
                    LOG.info("External ratings refresh skipped: no stale media rows")
                    await _persist_success(
                        state.id,
                        now,
                        provider_summary=_provider_summary(provider_states),
                        movie_count=0,
                        series_count=0,
                        request_count=0,
                        updated_count=0,
                    )
                    return

                updated_count = 0
                movie_count = 0
                series_count = 0
                for item in items:
                    if not _any_provider_available(provider_states):
                        break
                    changed = await _refresh_item(item, mdblist=mdblist, omdb=omdb)
                    if changed:
                        updated_count += 1
                    if item.media_type is MediaType.MOVIE:
                        movie_count += 1
                    else:
                        series_count += 1

                await db.commit()

            request_count = sum(
                provider.requests_used for provider in provider_states.values()
            )
            await _persist_success(
                state.id,
                now,
                provider_summary=_provider_summary(provider_states),
                movie_count=movie_count,
                series_count=series_count,
                request_count=request_count,
                updated_count=updated_count,
            )
            LOG.info(
                "External ratings refresh complete: "
                f"{updated_count} updated, {request_count} provider request(s)"
            )
        except Exception as exc:
            await _persist_error(state.id, now, str(exc))
            raise


async def _refresh_item(
    item: _MediaItem,
    *,
    mdblist: _ProviderState | None,
    omdb: _ProviderState | None,
) -> bool:
    row = item.row
    ratings = ExternalRatingValues()
    successful_sources: list[str] = []
    had_successful_request = False

    if mdblist and mdblist.available and row.tmdb_id:
        mdblist_client = MDBListClient(
            api_key=mdblist.api_key,
            base_url=mdblist.config.base_url or DEFAULT_MDBLIST_BASE_URL,
        )
        try:
            await mdblist.wait_for_next_request()
            mdblist.mark_request_attempted()
            provider_values = await mdblist_client.get_ratings(
                item.media_type, int(row.tmdb_id)
            )
            mdblist.update_rate_limit(mdblist_client.last_rate_limit)
            mdblist.requests_used += 1
            had_successful_request = True
            ratings.merge_missing(provider_values)
            if provider_values.has_any():
                successful_sources.append(Service.MDBLIST.value)
        except ProviderRateLimitError as exc:
            mdblist.update_rate_limit(exc.rate_limit)
            mdblist.disabled_reason = str(exc)
            LOG.warning(str(exc))
        except Exception as exc:
            mdblist.update_rate_limit(mdblist_client.last_rate_limit)
            mdblist.requests_used += 1
            LOG.debug(
                "MDBList rating lookup failed for "
                f"{item.media_type.value} TMDB {row.tmdb_id}: {exc}"
            )

    if omdb and omdb.available and row.imdb_id and _needs_omdb_fallback(ratings):
        omdb_client = OMDbClient(
            api_key=omdb.api_key,
            base_url=omdb.config.base_url or DEFAULT_OMDB_BASE_URL,
        )
        try:
            await omdb.wait_for_next_request()
            omdb.mark_request_attempted()
            provider_values = await omdb_client.get_ratings(str(row.imdb_id))
            omdb.update_rate_limit(omdb_client.last_rate_limit)
            omdb.requests_used += 1
            had_successful_request = True
            ratings.merge_missing(provider_values)
            if provider_values.has_any():
                successful_sources.append(Service.OMDB.value)
        except ProviderRateLimitError as exc:
            omdb.update_rate_limit(exc.rate_limit)
            omdb.disabled_reason = str(exc)
            LOG.warning(str(exc))
        except Exception as exc:
            omdb.update_rate_limit(omdb_client.last_rate_limit)
            omdb.requests_used += 1
            LOG.debug(
                "OMDb rating lookup failed for "
                f"{item.media_type.value} IMDb {row.imdb_id}: {exc}"
            )

    if not had_successful_request:
        return False

    source = "+".join(successful_sources) if successful_sources else "none"
    changed = row.external_ratings_source != source
    for field in _rating_model_fields():
        if getattr(row, field) != getattr(ratings, field):
            changed = True
            setattr(row, field, getattr(ratings, field))
    row.external_ratings_source = source
    row.external_ratings_refreshed_at = datetime.now(UTC)
    return changed


def _needs_omdb_fallback(ratings: ExternalRatingValues) -> bool:
    # OMDb does not currently expose Popcornmeter, but it is useful as a fallback
    # for Tomatometer and Metacritic when MDBList is disabled or incomplete.
    return (
        ratings.rottentomatoes_tomato_meter is None
        or ratings.metacritic_metascore is None
    )


def _rating_model_fields() -> tuple[str, ...]:
    return (
        "rottentomatoes_tomato_meter",
        "rottentomatoes_tomato_vote_count",
        "rottentomatoes_popcorn_meter",
        "rottentomatoes_popcorn_vote_count",
        "metacritic_metascore",
        "metacritic_vote_count",
        "metacritic_user_score",
        "metacritic_user_vote_count",
        "trakt_rating",
        "trakt_vote_count",
        "letterboxd_score",
        "letterboxd_vote_count",
    )


async def _load_stale_items(db: Any, *, now: datetime, limit: int) -> list[_MediaItem]:
    stale_before = now - timedelta(days=DEFAULT_STALE_AFTER_DAYS)
    movie_rows = (
        await db.execute(
            select(Movie)
            .where(
                or_(
                    Movie.external_ratings_refreshed_at.is_(None),
                    Movie.external_ratings_refreshed_at < stale_before,
                )
            )
            .order_by(
                Movie.external_ratings_refreshed_at.isnot(None),
                Movie.external_ratings_refreshed_at.asc(),
                Movie.id.asc(),
            )
            .limit(limit)
        )
    ).scalars()
    series_rows = (
        await db.execute(
            select(Series)
            .where(
                or_(
                    Series.external_ratings_refreshed_at.is_(None),
                    Series.external_ratings_refreshed_at < stale_before,
                )
            )
            .order_by(
                Series.external_ratings_refreshed_at.isnot(None),
                Series.external_ratings_refreshed_at.asc(),
                Series.id.asc(),
            )
            .limit(limit)
        )
    ).scalars()

    items = [
        *(_MediaItem(MediaType.MOVIE, row) for row in movie_rows),
        *(_MediaItem(MediaType.SERIES, row) for row in series_rows),
    ]
    items.sort(
        key=lambda item: (
            item.row.external_ratings_refreshed_at is not None,
            item.row.external_ratings_refreshed_at or datetime.min.replace(tzinfo=UTC),
            item.media_type.value,
            item.row.id,
        )
    )
    return items[:limit]


async def _load_provider_states() -> dict[Service, _ProviderState]:
    async with async_db() as db:
        configs = (
            await db.execute(
                select(ServiceConfig).where(
                    ServiceConfig.enabled.is_(True),
                    ServiceConfig.service_type.in_([Service.MDBLIST, Service.OMDB]),
                )
            )
        ).scalars()
        states: dict[Service, _ProviderState] = {}
        for config in configs:
            api_key = _config_api_key(config)
            default_limit = (
                DEFAULT_MDBLIST_REQUEST_LIMIT
                if config.service_type is Service.MDBLIST
                else DEFAULT_OMDB_REQUEST_LIMIT
            )
            states[config.service_type] = _ProviderState(
                config=config,
                api_key=api_key,
                request_limit=_request_limit(config, default_limit),
                request_delay_seconds=_request_delay_seconds(config),
            )
        return states


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


def _any_provider_available(providers: dict[Service, _ProviderState]) -> bool:
    return any(provider.available for provider in providers.values())


def _provider_summary(providers: dict[Service, _ProviderState]) -> dict[str, Any]:
    return {
        service.value: {
            "requests_used": provider.requests_used,
            "request_limit": provider.request_limit,
            "request_delay_seconds": provider.request_delay_seconds,
            "disabled_reason": provider.disabled_reason,
            "rate_limit": provider.rate_limit.to_dict()
            if provider.rate_limit is not None
            else None,
        }
        for service, provider in providers.items()
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
    checked_at: datetime,
    *,
    provider_summary: dict[str, Any],
    movie_count: int,
    series_count: int,
    request_count: int,
    updated_count: int,
) -> None:
    async with async_db() as db:
        state = await db.get(ExternalRatingsIngestState, state_id)
        if state is not None:
            state.provider_summary = provider_summary
            state.movie_count = movie_count
            state.series_count = series_count
            state.request_count = request_count
            state.updated_count = updated_count
            state.last_checked_at = checked_at
            state.last_successful_refresh_at = checked_at
            state.last_error = None
            await db.commit()


async def _persist_error(state_id: int, checked_at: datetime, error: str) -> None:
    async with async_db() as db:
        state = await db.get(ExternalRatingsIngestState, state_id)
        if state is not None:
            state.last_checked_at = checked_at
            state.last_error = (error or "unknown error")[:ERROR_MSG_MAX_LENGTH]
            await db.commit()
