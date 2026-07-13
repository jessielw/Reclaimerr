from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import niquests
from sqlalchemy import select
from sqlalchemy import update as sql_update

from backend.core.anilist import (
    ANIBRIDGE_RELEASE_API_URL,
    ANILIST_GRAPHQL_URL,
    build_conditional_headers,
    choose_series_anilist_id,
    extract_anilist_ids,
    parse_descriptor,
    sha256_hex,
)
from backend.core.logger import LOG
from backend.core.task_tracking import track_task_execution
from backend.database import async_db
from backend.database.models import AniListRatingsIngestState, Movie, Series
from backend.enums import Task

ERROR_MSG_MAX_LENGTH = 2000
ANILIST_BATCH_SIZE = 20
ANILIST_MIN_INTERVAL_SECONDS = 2.1
ANILIST_MAX_RETRIES = 5

__all__ = ["refresh_anilist_ratings"]


async def refresh_anilist_ratings() -> None:
    """Refresh AniBridge mappings + AniList metadata and denormalize onto media rows."""
    async with track_task_execution(Task.ANILIST_RATINGS_REFRESH):
        state = await _get_or_create_state()
        now = datetime.now(UTC)

        LOG.info("Refreshing AniList supplemental ratings")

        try:
            async with niquests.AsyncSession() as session:
                release = await _fetch_latest_release(session)
                mappings_url = _extract_mappings_asset_url(release)
                if not mappings_url:
                    raise RuntimeError("AniBridge release missing mappings.json asset")

                headers = build_conditional_headers(
                    etag=state.etag, last_modified=state.last_modified
                )
                mappings_response = await session.get(
                    mappings_url,
                    headers=headers,
                    timeout=180,
                )
                status_code = _safe_status_code(mappings_response.status_code)
                if status_code == 304:
                    await _persist_not_modified(state.id, now, mappings_url)
                    LOG.info("AniBridge mappings not modified (304)")
                    return
                mappings_response.raise_for_status()

                payload = _required_bytes(
                    mappings_response.content,
                    context="AniBridge mappings response content",
                )
                etag = _header_value(mappings_response.headers, "ETag")
                last_modified = _header_value(
                    mappings_response.headers,
                    "Last-Modified",
                )
                content_length = _parse_content_length(
                    _header_value(mappings_response.headers, "Content-Length"),
                    fallback=len(payload),
                )
                mappings = json.loads(payload.decode("utf-8"))

                source_to_anilist_ids = _build_source_to_anilist_map(mappings)
                (
                    movie_assignments,
                    series_assignments,
                    anilist_ids,
                ) = await _resolve_media_assignments(source_to_anilist_ids)

                anilist_meta_by_id = await _fetch_anilist_metadata(
                    session=session,
                    anilist_ids=sorted(anilist_ids),
                )

                await _persist_denormalized(
                    state_id=state.id,
                    now=now,
                    dataset_url=mappings_url,
                    etag=etag,
                    last_modified=last_modified,
                    content_length=content_length,
                    dataset_sha256=sha256_hex(payload),
                    row_count=len(source_to_anilist_ids),
                    movie_assignments=movie_assignments,
                    series_assignments=series_assignments,
                    anilist_meta_by_id=anilist_meta_by_id,
                )
                movie_count = len(movie_assignments)
                series_count = len(series_assignments)
                media_count = len(anilist_meta_by_id)
                LOG.info(
                    "AniList supplemental refresh complete "
                    f"(movies={movie_count}, series={series_count}, media={media_count})"
                )
                del mappings
                del source_to_anilist_ids
                del movie_assignments
                del series_assignments
                del anilist_ids
                del anilist_meta_by_id
                del payload
                del mappings_response
                del release
        except Exception as exc:
            await _persist_error(state.id, now, str(exc))
            raise


async def _fetch_latest_release(session: niquests.AsyncSession) -> dict[str, Any]:
    response = await session.get(ANIBRIDGE_RELEASE_API_URL, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected AniBridge release payload shape")
    return payload


def _extract_mappings_asset_url(release: dict[str, Any]) -> str | None:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None

    preferred_names = ("mappings.min.json", "mappings.json")
    for preferred_name in preferred_names:
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            if str(asset.get("name") or "").strip() != preferred_name:
                continue
            url = str(asset.get("browser_download_url") or "").strip()
            if url:
                return url
    return None


def _build_source_to_anilist_map(mappings: dict[str, Any]) -> dict[str, list[int]]:
    source_to_ids: dict[str, list[int]] = {}
    for raw_source_desc, targets in mappings.items():
        source_desc = str(raw_source_desc)
        if str(source_desc).startswith("$"):
            continue
        ids = extract_anilist_ids(targets)
        if not ids:
            continue
        parsed = _parse_source_descriptor(source_desc)
        if parsed is None:
            continue
        source_to_ids[parsed] = ids
    return source_to_ids


def _parse_source_descriptor(source_desc: str) -> str | None:
    parsed = parse_descriptor(source_desc)
    if parsed is None:
        return None
    return f"{parsed.provider}:{parsed.provider_id}" + (
        f":{parsed.scope}" if parsed.scope else ""
    )


async def _resolve_media_assignments(
    source_to_anilist_ids: dict[str, list[int]],
) -> tuple[dict[int, int], dict[int, int], set[int]]:
    movie_assignments: dict[int, int] = {}
    series_assignments: dict[int, int] = {}
    anilist_ids: set[int] = set()

    async with async_db() as db:
        movie_rows = (
            await db.execute(select(Movie.id, Movie.tmdb_id, Movie.imdb_id))
        ).all()
        for movie_id, tmdb_id, imdb_id in movie_rows:
            if movie_id is None:
                continue
            resolved = None
            if tmdb_id is not None:
                resolved_ids = source_to_anilist_ids.get(f"tmdb_movie:{tmdb_id}")
                if resolved_ids:
                    resolved = resolved_ids[0]
            if resolved is None and imdb_id:
                resolved_ids = source_to_anilist_ids.get(f"imdb_movie:{imdb_id}")
                if resolved_ids:
                    resolved = resolved_ids[0]
            if resolved is not None:
                resolved_int = int(resolved)
                movie_assignments[int(movie_id)] = resolved_int
                anilist_ids.add(resolved_int)

        series_rows = (
            await db.execute(select(Series.id, Series.tmdb_id, Series.imdb_id))
        ).all()
        for series_id, tmdb_id, imdb_id in series_rows:
            if series_id is None:
                continue
            resolved = choose_series_anilist_id(
                source_to_anilist_ids=source_to_anilist_ids,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
            )
            if resolved is not None:
                resolved_int = int(resolved)
                series_assignments[int(series_id)] = resolved_int
                anilist_ids.add(resolved_int)

    return movie_assignments, series_assignments, anilist_ids


async def _fetch_anilist_metadata(
    *,
    session: niquests.AsyncSession,
    anilist_ids: list[int],
) -> dict[int, dict[str, Any]]:
    if not anilist_ids:
        return {}

    results: dict[int, dict[str, Any]] = {}
    for index in range(0, len(anilist_ids), ANILIST_BATCH_SIZE):
        batch = anilist_ids[index : index + ANILIST_BATCH_SIZE]
        if index > 0:
            await asyncio.sleep(ANILIST_MIN_INTERVAL_SECONDS)
        payload = await _post_anilist_batch(session=session, anilist_ids=batch)
        for alias_data in payload.values():
            if not isinstance(alias_data, dict):
                continue
            media_id = alias_data.get("id")
            if not isinstance(media_id, int):
                continue
            results[media_id] = {
                "score": alias_data.get("averageScore"),
                "popularity": alias_data.get("popularity"),
                "favourites": alias_data.get("favourites"),
            }
    return results


async def _post_anilist_batch(
    *,
    session: niquests.AsyncSession,
    anilist_ids: list[int],
) -> dict[str, Any]:
    query_parts = []
    for i, anilist_id in enumerate(anilist_ids):
        query_parts.append(
            f"m{i}: Media(id: {anilist_id}, type: ANIME) "
            "{ id averageScore popularity favourites }"
        )
    query = "query {" + " ".join(query_parts) + "}"
    body = {"query": query}

    for attempt in range(ANILIST_MAX_RETRIES):
        response = await session.post(
            ANILIST_GRAPHQL_URL,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        status_code = _safe_status_code(response.status_code)
        if status_code == 429:
            await asyncio.sleep(_retry_after_seconds(response.headers))
            continue

        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected AniList GraphQL payload shape")
        errors = payload.get("errors")
        if isinstance(errors, list) and _has_429_error(errors):
            await asyncio.sleep(_retry_after_seconds(response.headers))
            continue

        data = payload.get("data")
        if not isinstance(data, dict):
            return {}
        return data

    raise RuntimeError("AniList GraphQL rate limited after maximum retries")


def _has_429_error(errors: list[Any]) -> bool:
    for err in errors:
        if not isinstance(err, dict):
            continue
        status = _coerce_int(err.get("status"))
        if status == 429:
            return True
    return False


def _retry_after_seconds(headers: Any) -> float:
    retry_after = _header_value(headers, "Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), 1.0)
        except ValueError:
            pass

    reset_header = _header_value(headers, "X-RateLimit-Reset")
    if reset_header:
        try:
            reset_ts = int(reset_header)
            delta = reset_ts - int(datetime.now(UTC).timestamp())
            if delta > 0:
                return float(delta)
        except ValueError:
            pass

    return 5.0


async def _persist_denormalized(
    *,
    state_id: int,
    now: datetime,
    dataset_url: str,
    etag: str | None,
    last_modified: str | None,
    content_length: int,
    dataset_sha256: str,
    row_count: int,
    movie_assignments: dict[int, int],
    series_assignments: dict[int, int],
    anilist_meta_by_id: dict[int, dict[str, Any]],
) -> None:
    async with async_db() as db:
        movie_rows = (await db.execute(select(Movie.id))).all()
        for (movie_id,) in movie_rows:
            assigned = movie_assignments.get(int(movie_id))
            if assigned is None:
                await db.execute(
                    sql_update(Movie)
                    .where(Movie.id == movie_id)
                    .values(
                        anilist_id=None,
                        anilist_score=None,
                        anilist_popularity=None,
                        anilist_favourites=None,
                        anilist_refreshed_at=None,
                    )
                )
                continue

            meta = anilist_meta_by_id.get(assigned, {})
            await db.execute(
                sql_update(Movie)
                .where(Movie.id == movie_id)
                .values(
                    anilist_id=assigned,
                    anilist_score=_coerce_int(meta.get("score")),
                    anilist_popularity=_coerce_int(meta.get("popularity")),
                    anilist_favourites=_coerce_int(meta.get("favourites")),
                    anilist_refreshed_at=now,
                )
            )

        series_rows = (await db.execute(select(Series.id))).all()
        for (series_id,) in series_rows:
            assigned = series_assignments.get(int(series_id))
            if assigned is None:
                await db.execute(
                    sql_update(Series)
                    .where(Series.id == series_id)
                    .values(
                        anilist_id=None,
                        anilist_score=None,
                        anilist_popularity=None,
                        anilist_favourites=None,
                        anilist_refreshed_at=None,
                    )
                )
                continue

            meta = anilist_meta_by_id.get(assigned, {})
            await db.execute(
                sql_update(Series)
                .where(Series.id == series_id)
                .values(
                    anilist_id=assigned,
                    anilist_score=_coerce_int(meta.get("score")),
                    anilist_popularity=_coerce_int(meta.get("popularity")),
                    anilist_favourites=_coerce_int(meta.get("favourites")),
                    anilist_refreshed_at=now,
                )
            )

        state = await db.get(AniListRatingsIngestState, state_id)
        if state is not None:
            state.dataset_url = dataset_url
            state.etag = etag
            state.last_modified = last_modified
            state.sha256 = dataset_sha256
            state.content_length = content_length
            state.row_count = row_count
            state.last_checked_at = now
            state.last_successful_refresh_at = now
            state.last_error = None

        await db.commit()


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_status_code(value: Any) -> int:
    parsed = _coerce_int(value)
    return parsed if parsed is not None else -1


def _required_bytes(value: bytes | None, *, context: str) -> bytes:
    if value is None:
        raise RuntimeError(f"{context} is missing")
    return value


async def _get_or_create_state() -> AniListRatingsIngestState:
    async with async_db() as db:
        row = (await db.execute(select(AniListRatingsIngestState))).scalars().first()
        if row is None:
            row = AniListRatingsIngestState(dataset_url="")
            db.add(row)
            await db.commit()
        return row


async def _persist_not_modified(
    state_id: int,
    checked_at: datetime,
    dataset_url: str,
) -> None:
    async with async_db() as db:
        state = await db.get(AniListRatingsIngestState, state_id)
        if state is not None:
            state.last_checked_at = checked_at
            state.last_error = None
            state.dataset_url = dataset_url
            await db.commit()


async def _persist_error(state_id: int, checked_at: datetime, error: str) -> None:
    async with async_db() as db:
        state = await db.get(AniListRatingsIngestState, state_id)
        if state is not None:
            state.last_checked_at = checked_at
            state.last_error = (error or "unknown error")[:ERROR_MSG_MAX_LENGTH]
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
