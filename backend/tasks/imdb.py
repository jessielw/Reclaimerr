from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import niquests
from sqlalchemy import select
from sqlalchemy import update as sql_update

from backend.core.imdb import (
    IMDB_TITLE_RATINGS_URL,
    build_conditional_headers,
    parse_imdb_last_modified,
    parse_title_ratings_tsv_gz,
    sha256_hex,
)
from backend.core.imdb_cache import (
    IMDbCacheMetadata,
    get_cached_ratings,
    persist_error,
    persist_not_modified,
    read_cache_state,
    replace_cache,
)
from backend.core.logger import LOG
from backend.core.task_tracking import track_task_execution
from backend.database import async_db
from backend.database.models import Movie, Series
from backend.enums import Task

ERROR_MSG_MAX_LENGTH = 2000
DENORMALIZE_COMMIT_BATCH_SIZE = 500

__all__ = ["refresh_imdb_ratings"]


async def refresh_imdb_ratings() -> None:
    """Refresh cached IMDb title ratings and denormalized ratings on media rows."""
    async with track_task_execution(Task.IMDB_RATINGS_REFRESH):
        state = read_cache_state()
        now = datetime.now(UTC)
        headers = build_conditional_headers(
            etag=state.etag if state else None,
            last_modified=state.last_modified if state else None,
        )

        LOG.info("Refreshing IMDb ratings dataset")

        try:
            response = await _fetch_dataset(headers)
            status_code = int(response.status_code)
            if status_code == 304:
                persist_not_modified(now)
                await _denormalize_media_imdb_ratings(now)
                LOG.info("IMDb ratings dataset not modified (304)")
                return

            response.raise_for_status()

            payload = response.content
            etag = _header_value(response.headers, "ETag")
            last_modified = _header_value(response.headers, "Last-Modified")
            content_length = _parse_content_length(
                _header_value(response.headers, "Content-Length"),
                fallback=len(payload),
            )

            processed_rows = replace_cache(
                parse_title_ratings_tsv_gz(payload),
                IMDbCacheMetadata(
                    dataset_url=IMDB_TITLE_RATINGS_URL,
                    etag=etag,
                    last_modified=last_modified,
                    sha256=sha256_hex(payload),
                    content_length=content_length,
                    row_count=0,
                    last_checked_at=now,
                    last_successful_refresh_at=now,
                    source_updated_at=parse_imdb_last_modified(last_modified),
                    last_error=None,
                ),
            )
            await _denormalize_media_imdb_ratings(now)
            del payload
            del response
            LOG.info(f"IMDb ratings refresh complete ({processed_rows} rows)")
        except Exception as exc:
            persist_error(now, str(exc), max_length=ERROR_MSG_MAX_LENGTH)
            raise


async def _fetch_dataset(headers: dict[str, str]) -> Any:
    async with niquests.AsyncSession() as session:
        return await session.get(
            IMDB_TITLE_RATINGS_URL,
            headers=headers,
            timeout=180,
        )


async def _denormalize_media_imdb_ratings(refreshed_at: datetime) -> None:
    movie_changes = 0
    series_changes = 0

    async with async_db() as db:
        movie_rows = (
            await db.execute(
                select(
                    Movie.id,
                    Movie.imdb_id,
                    Movie.imdb_rating,
                    Movie.imdb_vote_count,
                    Movie.imdb_ratings_refreshed_at,
                )
            )
        ).all()
        movie_rating_rows = get_cached_ratings(
            row.imdb_id for row in movie_rows if row.imdb_id
        )
        pending_updates = 0
        for row in movie_rows:
            cached = movie_rating_rows.get(row.imdb_id) if row.imdb_id else None
            rating = cached.rating if cached is not None else None
            vote_count = cached.vote_count if cached is not None else None
            refreshed_value = refreshed_at if cached is not None else None
            if not _needs_denormalized_update(
                current_rating=row.imdb_rating,
                current_vote_count=row.imdb_vote_count,
                current_refreshed_at=row.imdb_ratings_refreshed_at,
                next_rating=rating,
                next_vote_count=vote_count,
                has_cached_rating=cached is not None,
            ):
                continue
            await db.execute(
                sql_update(Movie)
                .where(Movie.id == row.id)
                .values(
                    imdb_rating=rating,
                    imdb_vote_count=vote_count,
                    imdb_ratings_refreshed_at=refreshed_value,
                )
            )
            movie_changes += 1
            pending_updates += 1
            if pending_updates >= DENORMALIZE_COMMIT_BATCH_SIZE:
                await db.commit()
                pending_updates = 0

        series_rows = (
            await db.execute(
                select(
                    Series.id,
                    Series.imdb_id,
                    Series.imdb_rating,
                    Series.imdb_vote_count,
                    Series.imdb_ratings_refreshed_at,
                )
            )
        ).all()
        series_rating_rows = get_cached_ratings(
            row.imdb_id for row in series_rows if row.imdb_id
        )
        for row in series_rows:
            cached = series_rating_rows.get(row.imdb_id) if row.imdb_id else None
            rating = cached.rating if cached is not None else None
            vote_count = cached.vote_count if cached is not None else None
            refreshed_value = refreshed_at if cached is not None else None
            if not _needs_denormalized_update(
                current_rating=row.imdb_rating,
                current_vote_count=row.imdb_vote_count,
                current_refreshed_at=row.imdb_ratings_refreshed_at,
                next_rating=rating,
                next_vote_count=vote_count,
                has_cached_rating=cached is not None,
            ):
                continue
            await db.execute(
                sql_update(Series)
                .where(Series.id == row.id)
                .values(
                    imdb_rating=rating,
                    imdb_vote_count=vote_count,
                    imdb_ratings_refreshed_at=refreshed_value,
                )
            )
            series_changes += 1
            pending_updates += 1
            if pending_updates >= DENORMALIZE_COMMIT_BATCH_SIZE:
                await db.commit()
                pending_updates = 0

        if pending_updates:
            await db.commit()

    LOG.info(
        "IMDb ratings denormalization complete "
        f"({movie_changes} movie row(s), {series_changes} series row(s) updated)"
    )


def _ratings_equal(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return abs(left - right) < 0.000001


def _needs_denormalized_update(
    *,
    current_rating: float | None,
    current_vote_count: int | None,
    current_refreshed_at: datetime | None,
    next_rating: float | None,
    next_vote_count: int | None,
    has_cached_rating: bool,
) -> bool:
    if not _ratings_equal(current_rating, next_rating):
        return True
    if current_vote_count != next_vote_count:
        return True
    if has_cached_rating:
        return current_refreshed_at is None
    return current_refreshed_at is not None


def _header_value(headers: Any, key: str) -> str | None:
    value = None
    if headers is not None:
        value = headers.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_content_length(raw: str | None, fallback: int) -> int:
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return int(fallback)
