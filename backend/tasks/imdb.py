from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import niquests
from sqlalchemy import case, exists, func, select
from sqlalchemy import delete as sql_delete
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.imdb import (
    IMDB_TITLE_RATINGS_URL,
    batched_rows,
    build_conditional_headers,
    parse_imdb_last_modified,
    parse_title_ratings_tsv_gz,
    sha256_hex,
)
from backend.core.logger import LOG
from backend.core.task_tracking import track_task_execution
from backend.database import async_db
from backend.database.models import (
    IMDbRatingsIngestState,
    IMDbTitleRating,
    Movie,
    Series,
)
from backend.enums import Task

UPSERT_BATCH_SIZE = 5000
ERROR_MSG_MAX_LENGTH = 2000

__all__ = ["refresh_imdb_ratings"]


async def refresh_imdb_ratings() -> None:
    """Refresh cached IMDb title ratings and denormalized ratings on media rows."""
    async with track_task_execution(Task.IMDB_RATINGS_REFRESH):
        state = await _get_or_create_state()
        now = datetime.now(UTC)
        headers = build_conditional_headers(
            etag=state.etag, last_modified=state.last_modified
        )

        LOG.info("Refreshing IMDb ratings dataset")

        try:
            response = await _fetch_dataset(headers)
            status_code = int(response.status_code)
            if status_code == 304:
                await _persist_not_modified(state.id, now)
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
            source_updated_at = parse_imdb_last_modified(last_modified)

            processed_rows = await _ingest_payload(
                state_id=state.id,
                payload=payload,
                now=now,
                etag=etag,
                last_modified=last_modified,
                content_length=content_length,
                source_updated_at=source_updated_at,
            )
            LOG.info(f"IMDb ratings refresh complete ({processed_rows} rows)")
        except Exception as exc:
            await _persist_error(state.id, now, str(exc))
            raise


async def _fetch_dataset(headers: dict[str, str]) -> Any:
    async with niquests.AsyncSession() as session:
        return await session.get(
            IMDB_TITLE_RATINGS_URL,
            headers=headers,
            timeout=180,
        )


async def _ingest_payload(
    *,
    state_id: int,
    payload: bytes,
    now: datetime,
    etag: str | None,
    last_modified: str | None,
    content_length: int,
    source_updated_at: datetime | None,
) -> int:
    dataset_sha256 = sha256_hex(payload)
    processed_rows = 0

    async with async_db() as db:
        for batch in batched_rows(
            parse_title_ratings_tsv_gz(payload), UPSERT_BATCH_SIZE
        ):
            values = [
                {
                    "imdb_id": row.imdb_id,
                    "rating": row.rating,
                    "vote_count": row.vote_count,
                    "source_updated_at": source_updated_at,
                    "refreshed_at": now,
                }
                for row in batch
            ]
            if not values:
                continue

            stmt = sqlite_insert(IMDbTitleRating).values(values)
            upsert_stmt = stmt.on_conflict_do_update(
                index_elements=["imdb_id"],
                set_={
                    "rating": stmt.excluded.rating,
                    "vote_count": stmt.excluded.vote_count,
                    "source_updated_at": stmt.excluded.source_updated_at,
                    "refreshed_at": stmt.excluded.refreshed_at,
                    "updated_at": func.now(),
                },
            )
            await db.execute(upsert_stmt)
            processed_rows += len(values)

        await db.execute(
            sql_delete(IMDbTitleRating).where(IMDbTitleRating.refreshed_at != now)
        )

        await _denormalize_media_imdb_ratings(db, now)

        state = await db.get(IMDbRatingsIngestState, state_id)
        if state is not None:
            state.dataset_url = IMDB_TITLE_RATINGS_URL
            state.etag = etag
            state.last_modified = last_modified
            state.sha256 = dataset_sha256
            state.content_length = content_length
            state.row_count = processed_rows
            state.last_checked_at = now
            state.last_successful_refresh_at = now
            state.last_error = None

        await db.commit()

    return processed_rows


async def _denormalize_media_imdb_ratings(
    db: AsyncSession, refreshed_at: datetime
) -> None:
    movie_rating_subq = (
        select(IMDbTitleRating.rating)
        .where(IMDbTitleRating.imdb_id == Movie.imdb_id)
        .scalar_subquery()
    )
    movie_votes_subq = (
        select(IMDbTitleRating.vote_count)
        .where(IMDbTitleRating.imdb_id == Movie.imdb_id)
        .scalar_subquery()
    )
    movie_exists = exists(
        select(IMDbTitleRating.id).where(IMDbTitleRating.imdb_id == Movie.imdb_id)
    )
    await db.execute(
        sql_update(Movie)
        .where(Movie.imdb_id.is_not(None))
        .values(
            imdb_rating=movie_rating_subq,
            imdb_vote_count=movie_votes_subq,
            imdb_ratings_refreshed_at=case((movie_exists, refreshed_at), else_=None),
        )
    )
    await db.execute(
        sql_update(Movie)
        .where(Movie.imdb_id.is_(None))
        .values(
            imdb_rating=None,
            imdb_vote_count=None,
            imdb_ratings_refreshed_at=None,
        )
    )

    series_rating_subq = (
        select(IMDbTitleRating.rating)
        .where(IMDbTitleRating.imdb_id == Series.imdb_id)
        .scalar_subquery()
    )
    series_votes_subq = (
        select(IMDbTitleRating.vote_count)
        .where(IMDbTitleRating.imdb_id == Series.imdb_id)
        .scalar_subquery()
    )
    series_exists = exists(
        select(IMDbTitleRating.id).where(IMDbTitleRating.imdb_id == Series.imdb_id)
    )
    await db.execute(
        sql_update(Series)
        .where(Series.imdb_id.is_not(None))
        .values(
            imdb_rating=series_rating_subq,
            imdb_vote_count=series_votes_subq,
            imdb_ratings_refreshed_at=case((series_exists, refreshed_at), else_=None),
        )
    )
    await db.execute(
        sql_update(Series)
        .where(Series.imdb_id.is_(None))
        .values(
            imdb_rating=None,
            imdb_vote_count=None,
            imdb_ratings_refreshed_at=None,
        )
    )


async def _get_or_create_state() -> IMDbRatingsIngestState:
    async with async_db() as db:
        row = (await db.execute(select(IMDbRatingsIngestState))).scalars().first()
        if row is None:
            row = IMDbRatingsIngestState(dataset_url=IMDB_TITLE_RATINGS_URL)
            db.add(row)
            await db.commit()
        return row


async def _persist_not_modified(state_id: int, checked_at: datetime) -> None:
    async with async_db() as db:
        state = await db.get(IMDbRatingsIngestState, state_id)
        if state is not None:
            state.last_checked_at = checked_at
            state.last_error = None
            state.dataset_url = IMDB_TITLE_RATINGS_URL
            await db.commit()


async def _persist_error(state_id: int, checked_at: datetime, error: str) -> None:
    async with async_db() as db:
        state = await db.get(IMDbRatingsIngestState, state_id)
        if state is not None:
            state.last_checked_at = checked_at
            state.last_error = (error or "unknown error")[:ERROR_MSG_MAX_LENGTH]
            state.dataset_url = IMDB_TITLE_RATINGS_URL
            await db.commit()


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
