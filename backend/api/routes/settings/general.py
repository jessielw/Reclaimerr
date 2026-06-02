from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.utils.filesystem import normalize_fpath
from backend.database import get_db
from backend.database.models import (
    GeneralSettings,
    MediaFavorite,
    MediaWatchUser,
    Movie,
    MovieVersion,
    Series,
    SeriesServiceRef,
    ServiceConfig,
    TaskSchedule,
    User,
)
from backend.enums import MediaType, Service, Task
from backend.models.post_action_webhooks import PostActionWebhookEvent
from backend.models.settings import (
    FavoritesMediaEntryResponse,
    FavoritesUserLookupResponse,
    GeneralSettingsResponse,
    PaginatedFavoritesMediaResponse,
    PostActionWebhookTestRequest,
    PostActionWebhookTestResponse,
    WatchUserLookupResponse,
)
from backend.services.media_favorites_cache import media_favorites_snapshot_cache
from backend.services.media_watch_snapshot_cache import media_watch_snapshot_cache
from backend.services.post_action_webhooks import (
    invalidate_webhook_config_cache,
    send_post_action_webhook,
)
from backend.utils.helpers import normalize_leaving_soon_collection_title

router = APIRouter(tags=["settings", "general"])


_LEAVING_SOON_MEDIA_SERVICES = {
    Service.PLEX,
    Service.JELLYFIN,
    Service.EMBY,
}


def _normalize_leaving_soon_last_success_titles(
    raw_titles: object,
) -> dict[Service, str]:
    if not isinstance(raw_titles, Mapping):
        return {}
    normalized_titles: dict[Service, str] = {}
    for raw_service, raw_title in raw_titles.items():
        try:
            service = Service(str(raw_service))
        except Exception:
            continue
        if service not in _LEAVING_SOON_MEDIA_SERVICES:
            continue
        normalized_titles[service] = normalize_leaving_soon_collection_title(
            str(raw_title)
        )
    return normalized_titles


async def _cleanup_leaving_soon_collections_on_disable(
    settings: GeneralSettings,
) -> None:
    normalized_titles = _normalize_leaving_soon_last_success_titles(
        settings.leaving_soon_last_success_titles
    )
    if not normalized_titles:
        return

    updated_titles = dict(normalized_titles)
    titles_changed = False
    service_clients: list[tuple[Service, Any]] = [
        (Service.PLEX, service_manager.plex),
        (Service.JELLYFIN, service_manager.jellyfin),
        (Service.EMBY, service_manager.emby),
    ]
    for service_type, service_client in service_clients:
        previous_success_title = updated_titles.get(service_type)
        if previous_success_title is None:
            continue
        if service_client is None:
            continue

        delete_method = getattr(service_client, "delete_leaving_soon_collections", None)
        if not callable(delete_method):
            LOG.warning(
                "Leaving Soon cleanup method missing for "
                f"{service_type.value}; cannot remove title "
                f"{previous_success_title!r} on disable"
            )
            continue
        delete_func = cast(Callable[..., Awaitable[Any]], delete_method)
        try:
            await delete_func(base_title=previous_success_title)
        except Exception as e:
            LOG.warning(
                "Failed cleaning Leaving Soon collections for "
                f"{service_type.value} on disable (title "
                f"{previous_success_title!r}): {e}"
            )
            continue

        del updated_titles[service_type]
        titles_changed = True

    if not titles_changed:
        return
    settings.leaving_soon_last_success_titles = {
        service.value: title
        for service, title in updated_titles.items()
        if service in _LEAVING_SOON_MEDIA_SERVICES
    }


@router.get("/general")
async def get_general_settings(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GeneralSettingsResponse:
    """
    Get general settings.

    `updated_by` will be null if settings have never been updated since creation.
    """
    result = await db.execute(select(GeneralSettings))
    settings = result.scalars().first()
    # create default settings if not exist
    if not settings:
        settings = GeneralSettings()
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return GeneralSettingsResponse.model_validate(settings)


@router.put("/general")
async def update_general_settings(
    request: GeneralSettingsResponse,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GeneralSettingsResponse:
    """Update general settings."""
    result = await db.execute(select(GeneralSettings))
    settings = result.scalars().first()

    # should always exist since we create default on get, but just in case
    if not settings:
        raise HTTPException(status_code=404, detail="General settings not found")

    current_leaving_soon_title = normalize_leaving_soon_collection_title(
        request.leaving_soon_collection_title
    )
    was_auto_delete_enabled = bool(settings.auto_delete_enabled)
    was_leaving_soon_enabled = bool(settings.leaving_soon_enabled)
    should_disable_auto_delete_task = False
    delete_task_schedule_type = None
    delete_task_schedule_value = None

    if was_auto_delete_enabled and not request.auto_delete_enabled:
        delete_task_schedule = (
            await db.execute(
                select(TaskSchedule).where(
                    TaskSchedule.task == Task.DELETE_CLEANUP_CANDIDATES
                )
            )
        ).scalar_one_or_none()
        if delete_task_schedule is not None and delete_task_schedule.enabled:
            should_disable_auto_delete_task = True
            delete_task_schedule_type = delete_task_schedule.schedule_type
            delete_task_schedule_value = delete_task_schedule.schedule_value

    # update fields
    settings.worker_poll_min_seconds = request.worker_poll_min_seconds
    settings.worker_poll_max_seconds = request.worker_poll_max_seconds
    settings.path_mappings = [m.model_dump() for m in request.path_mappings]
    settings.post_action_webhooks = [
        w.model_dump(mode="json") for w in request.post_action_webhooks
    ]
    settings.move_enabled = request.move_enabled
    settings.move_destination_movies = request.move_destination_movies or None
    settings.move_destination_series = request.move_destination_series or None
    settings.media_server_fallback_enabled = request.media_server_fallback_enabled
    settings.default_arr_delete_behavior = request.default_arr_delete_behavior
    settings.add_arr_import_exclusions_on_delete = (
        request.add_arr_import_exclusions_on_delete
    )
    settings.auto_delete_enabled = request.auto_delete_enabled
    settings.favorites_ignore_enabled = request.favorites_ignore_enabled
    settings.favorites_protect_all_users = request.favorites_protect_all_users
    settings.favorites_usernames = request.favorites_usernames
    settings.requester_watch_user_mappings = [
        mapping.model_dump(mode="json")
        for mapping in request.requester_watch_user_mappings
    ]
    settings.leaving_soon_enabled = request.leaving_soon_enabled
    settings.leaving_soon_collection_title = current_leaving_soon_title
    if was_leaving_soon_enabled and not settings.leaving_soon_enabled:
        await _cleanup_leaving_soon_collections_on_disable(settings)

    # update metadata
    settings.updated_at = datetime.now(UTC)
    settings.updated_by_user_id = admin.id

    db.add(settings)
    await db.commit()
    await db.refresh(settings)
    if (
        should_disable_auto_delete_task
        and delete_task_schedule_type is not None
        and delete_task_schedule_value is not None
    ):
        from backend.scheduler import update_task_schedule

        await update_task_schedule(
            task=Task.DELETE_CLEANUP_CANDIDATES,
            schedule_type=delete_task_schedule_type,
            schedule_value=delete_task_schedule_value,
            enabled=False,
        )
    invalidate_webhook_config_cache()
    return GeneralSettingsResponse.model_validate(settings)


@router.get(
    "/general/favorites-users", response_model=list[FavoritesUserLookupResponse]
)
async def get_favorites_users(
    _admin: Annotated[User, Depends(require_admin)],
    refresh: Annotated[bool, Query()] = False,
) -> list[FavoritesUserLookupResponse]:
    """Get users for media favorites settings."""
    users = await media_favorites_snapshot_cache.get_favorites_user_lookup(
        force_refresh=refresh
    )
    return [FavoritesUserLookupResponse.model_validate(item) for item in users]


@router.get("/general/watch-users", response_model=list[WatchUserLookupResponse])
async def get_watch_users(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    refresh: Annotated[bool, Query()] = False,
) -> list[WatchUserLookupResponse]:
    """Get distinct known watch-user keys from media watch snapshots."""
    if refresh:
        ok, error = await media_watch_snapshot_cache.refresh_snapshot()
        if not ok:
            raise HTTPException(
                status_code=503,
                detail=error or "Failed to refresh watch snapshot",
            )

    rows = (
        await db.execute(
            select(
                MediaWatchUser.source_service,
                MediaWatchUser.watch_user_key,
                MediaWatchUser.watch_user_key_normalized,
            )
            .distinct()
            .order_by(
                func.lower(MediaWatchUser.watch_user_key_normalized).asc(),
                func.lower(MediaWatchUser.watch_user_key).asc(),
                MediaWatchUser.source_service.asc(),
            )
        )
    ).all()

    by_key: dict[str, WatchUserLookupResponse] = {}
    for source_service, user_key, user_key_normalized in rows:
        normalized = str(user_key_normalized or "").strip().lower()
        if not normalized:
            continue
        display_key = str(user_key or "").strip() or normalized
        existing = by_key.get(normalized)
        if existing is None:
            by_key[normalized] = WatchUserLookupResponse(
                user_key=display_key,
                user_key_normalized=normalized,
                source_services=[source_service],
            )
            continue
        if source_service not in existing.source_services:
            existing.source_services.append(source_service)

    result = list(by_key.values())
    for item in result:
        item.source_services = sorted(
            item.source_services,
            key=lambda service: str(service.value),
        )
    return result


@router.get("/general/favorites-media", response_model=PaginatedFavoritesMediaResponse)
async def get_favorites_media(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    search: Annotated[str | None, Query(max_length=200)] = None,
    media_type: Annotated[MediaType | None, Query()] = None,
    username: Annotated[str | None, Query(max_length=255)] = None,
    refresh: Annotated[bool, Query()] = False,
) -> PaginatedFavoritesMediaResponse:
    """Get paginated favorite media snapshot entries."""
    await media_favorites_snapshot_cache.ensure_fresh_snapshot(force=refresh)

    filters = []
    if media_type is not None:
        filters.append(MediaFavorite.media_type == media_type)

    username_filter = (username or "").strip().lower()
    if username_filter:
        filters.append(
            func.lower(MediaFavorite.username_normalized).contains(username_filter)
        )

    search_filter = (search or "").strip().lower()
    title_expr = func.coalesce(Movie.title, Series.title)
    if search_filter:
        filters.append(
            or_(
                func.lower(func.coalesce(Movie.title, "")).contains(search_filter),
                func.lower(func.coalesce(Series.title, "")).contains(search_filter),
            )
        )

    base_query = (
        select(
            MediaFavorite.media_type.label("media_type"),
            MediaFavorite.tmdb_id.label("tmdb_id"),
            Movie.title.label("movie_title"),
            Movie.year.label("movie_year"),
            Movie.poster_url.label("movie_poster_url"),
            Series.title.label("series_title"),
            Series.year.label("series_year"),
            Series.poster_url.label("series_poster_url"),
            func.count(func.distinct(MediaFavorite.username_normalized)).label(
                "favorite_user_count"
            ),
        )
        .outerjoin(
            Movie,
            and_(
                MediaFavorite.media_type == MediaType.MOVIE,
                MediaFavorite.tmdb_id == Movie.tmdb_id,
            ),
        )
        .outerjoin(
            Series,
            and_(
                MediaFavorite.media_type == MediaType.SERIES,
                MediaFavorite.tmdb_id == Series.tmdb_id,
            ),
        )
        .where(*filters)
        .group_by(
            MediaFavorite.media_type,
            MediaFavorite.tmdb_id,
            Movie.title,
            Movie.year,
            Movie.poster_url,
            Series.title,
            Series.year,
            Series.poster_url,
        )
    )

    total_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = int(total_result.scalar() or 0)

    offset = (page - 1) * per_page
    rows = (
        await db.execute(
            base_query.order_by(
                func.count(func.distinct(MediaFavorite.username_normalized)).desc(),
                func.lower(func.coalesce(title_expr, "")).asc(),
                MediaFavorite.tmdb_id.asc(),
            )
            .offset(offset)
            .limit(per_page)
        )
    ).all()

    key_pairs = [(row.media_type, row.tmdb_id) for row in rows]
    favorites_users_by_key: dict[tuple[MediaType, int], set[str]] = {}
    if key_pairs:
        username_rows = (
            await db.execute(
                select(
                    MediaFavorite.media_type,
                    MediaFavorite.tmdb_id,
                    MediaFavorite.username,
                ).where(
                    tuple_(MediaFavorite.media_type, MediaFavorite.tmdb_id).in_(
                        key_pairs
                    )
                )
            )
        ).all()

        for item_media_type, item_tmdb_id, item_username in username_rows:
            key = (item_media_type, int(item_tmdb_id))
            if key not in favorites_users_by_key:
                favorites_users_by_key[key] = set()
            if item_username:
                favorites_users_by_key[key].add(str(item_username).strip())

    items: list[FavoritesMediaEntryResponse] = []
    for row in rows:
        if row.media_type == MediaType.MOVIE:
            title = row.movie_title
            year = row.movie_year
            poster_url = row.movie_poster_url
        else:
            title = row.series_title
            year = row.series_year
            poster_url = row.series_poster_url

        is_missing_metadata = not bool(title)
        display_title = (
            str(title).strip() if title else f"Unknown Media (TMDB {int(row.tmdb_id)})"
        )
        key = (row.media_type, int(row.tmdb_id))
        favorite_users = sorted(
            favorites_users_by_key.get(key, set()),
            key=lambda value: value.lower(),
        )

        items.append(
            FavoritesMediaEntryResponse(
                media_type=row.media_type,
                tmdb_id=int(row.tmdb_id),
                title=display_title,
                year=year,
                poster_url=poster_url,
                favorite_user_count=int(row.favorite_user_count or 0),
                favorite_users=favorite_users,
                is_missing_metadata=is_missing_metadata,
            )
        )

    total_pages = (total + per_page - 1) // per_page if total else 0
    return PaginatedFavoritesMediaResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.post(
    "/general/webhooks/test",
    response_model=PostActionWebhookTestResponse,
)
async def test_post_action_webhook(
    request: PostActionWebhookTestRequest,
    _admin: Annotated[User, Depends(require_admin)],
) -> PostActionWebhookTestResponse:
    """Send a sample post action webhook without saving it."""
    result = await send_post_action_webhook(
        request.webhook.model_dump(mode="json"),
        PostActionWebhookEvent(
            action="deleted",
            media_type=MediaType.MOVIE,
            title="Reclaimerr Test Movie",
            tmdb_id=550,
            candidate_id=0,
            path="/media/movies/Reclaimerr Test Movie (1999)/movie.mkv",
            local_path="/mnt/media/movies/Reclaimerr Test Movie (1999)/movie.mkv",
            service_type="plex",
            movie_version_id=0,
        ),
    )
    return PostActionWebhookTestResponse(**result)


def _library_root(path: str) -> str | None:
    """Return the likely library-root prefix of a media file path.

    Movies/series are typically stored two levels deep inside a library:
      <library_root>/<Title (Year)>/<file.mkv>

    So we walk up two levels from the file.  Works for both POSIX and
    Windows-style paths; always normalizes the result to forward slashes.
    """
    # filter to prevent too short/empty results that aren't useful as suggestions
    NO_RETURN = {".", "/", ""}

    # detect path style and parse accordingly
    p_win = PureWindowsPath(path)
    p_pos = PurePosixPath(path)

    # prefer Windows if the path contains a drive letter or UNC prefix
    if p_win.drive:
        p = p_win
    elif path.startswith("/"):
        p = p_pos
    else:
        # try both, pick whichever has more parts
        p = p_win if len(p_win.parts) >= len(p_pos.parts) else p_pos

    parent = p.parent.parent  # strip filename + title folder
    result = normalize_fpath(parent)
    return result if result not in NO_RETURN else None


@router.get("/general/path-suggestions", response_model=list[str])
async def get_path_suggestions(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[str]:
    """Return a deduplicated list of likely library-root path prefixes
    derived from ingested MovieVersion and SeriesServiceRef paths.

    Designed to be cheap: fetches at most 100 distinct paths per table.
    """
    movie_result = await db.execute(
        select(MovieVersion.path)
        .where(MovieVersion.path.isnot(None))
        .distinct()
        .limit(100)
    )
    series_result = await db.execute(
        select(SeriesServiceRef.path)
        .where(SeriesServiceRef.path.isnot(None))
        .distinct()
        .limit(100)
    )

    raw_paths: list[str] = [r[0] for r in movie_result.all() if r[0]] + [
        r[0] for r in series_result.all() if r[0]
    ]

    suggestions: set[str] = set()
    for path in raw_paths:
        root = _library_root(path)
        if root:
            suggestions.add(root)

    return sorted(suggestions, key=lambda s: len(s))


@router.get("/general/path-mapping-scopes")
async def get_path_mapping_scopes(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """Return service configs that path mappings can be scoped to."""
    result = await db.execute(
        select(
            ServiceConfig.id,
            ServiceConfig.service_type,
            ServiceConfig.name,
            ServiceConfig.enabled,
        ).order_by(ServiceConfig.service_type, ServiceConfig.name)
    )
    return [
        {
            "id": config_id,
            "service_type": service_type,
            "name": name,
            "enabled": enabled,
        }
        for config_id, service_type, name, enabled in result.all()
    ]
