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
    User,
    WatchUserAlias,
)
from backend.enums import MediaType, Service
from backend.models.settings import (
    FavoritesMediaEntryResponse,
    FavoritesUserLookupResponse,
    GeneralSettingsResponse,
    PaginatedFavoritesMediaResponse,
    WatchUserLookupResponse,
)
from backend.services.media_favorites_cache import media_favorites_snapshot_cache
from backend.services.media_watch_snapshot_cache import media_watch_snapshot_cache
from backend.services.watch_identity import merge_directory_accounts
from backend.utils.helpers import normalize_leaving_soon_collection_title

router = APIRouter(tags=["settings", "general"])


_LEAVING_SOON_MEDIA_SERVICES = {
    Service.PLEX,
    Service.JELLYFIN,
    Service.EMBY,
}


async def _normalize_leaving_soon_last_success_titles(
    db: AsyncSession,
    raw_titles: object,
) -> dict[int, str]:
    """Normalize the persisted last-success-title map to `{service_config_id: title}`.

    Tolerates the pre-multi-instance shape (`{service_type: title}`) on read: a
    key that isn't a valid config id is resolved to whichever ServiceConfig
    currently has that service_type, so upgrading loses no in-flight state.
    Callers should always write the new shape back.
    """
    if not isinstance(raw_titles, Mapping):
        return {}
    normalized_titles: dict[int, str] = {}
    legacy_type_titles: dict[Service, str] = {}
    for raw_key, raw_title in raw_titles.items():
        title = normalize_leaving_soon_collection_title(str(raw_title))
        try:
            normalized_titles[int(raw_key)] = title
            continue
        except (TypeError, ValueError):
            pass
        try:
            service = Service(str(raw_key))
        except Exception:
            continue
        if service in _LEAVING_SOON_MEDIA_SERVICES:
            legacy_type_titles[service] = title

    if legacy_type_titles:
        rows = (
            await db.execute(
                select(ServiceConfig.id, ServiceConfig.service_type).where(
                    ServiceConfig.service_type.in_(legacy_type_titles.keys())
                )
            )
        ).all()
        config_id_by_type: dict[Service, int] = {}
        for config_id, service_type in rows:
            config_id_by_type.setdefault(service_type, config_id)
        for service, title in legacy_type_titles.items():
            config_id = config_id_by_type.get(service)
            if config_id is not None and config_id not in normalized_titles:
                normalized_titles[config_id] = title

    return normalized_titles


async def _cleanup_leaving_soon_collections_on_disable(
    db: AsyncSession,
    settings: GeneralSettings,
) -> None:
    normalized_titles = await _normalize_leaving_soon_last_success_titles(
        db, settings.leaving_soon_last_success_titles
    )
    if not normalized_titles:
        return

    updated_titles = dict(normalized_titles)
    titles_changed = False
    configs = (
        (
            await db.execute(
                select(ServiceConfig).where(
                    ServiceConfig.service_type.in_(_LEAVING_SOON_MEDIA_SERVICES),
                    ServiceConfig.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    configs_by_id = {config.id: config for config in configs}

    for config_id, previous_success_title in list(updated_titles.items()):
        if (config := configs_by_id.get(config_id)) is None:
            continue
        if (service_client := service_manager.get_media_server(
            config.service_type, config.id
        )) is None:
            continue

        delete_method = getattr(service_client, "delete_leaving_soon_collections", None)
        if not callable(delete_method):
            LOG.warning(
                "Leaving Soon cleanup method missing for "
                f"{config.service_type.value} (config {config_id}); cannot remove "
                f"title {previous_success_title!r} on disable"
            )
            continue
        delete_func = cast(Callable[..., Awaitable[Any]], delete_method)
        try:
            await delete_func(base_title=previous_success_title)
        except Exception as e:
            LOG.warning(
                "Failed cleaning Leaving Soon collections for "
                f"{config.service_type.value} (config {config_id}) on disable "
                f"(title {previous_success_title!r}): {e}"
            )
            continue

        del updated_titles[config_id]
        titles_changed = True

    if not titles_changed:
        return
    settings.leaving_soon_last_success_titles = {
        str(config_id): title for config_id, title in updated_titles.items()
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
    was_leaving_soon_enabled = bool(settings.leaving_soon_enabled)

    # update fields
    settings.worker_poll_min_seconds = request.worker_poll_min_seconds
    settings.worker_poll_max_seconds = request.worker_poll_max_seconds
    settings.path_mappings = [m.model_dump() for m in request.path_mappings]
    settings.move_destination_movies = request.move_destination_movies or None
    settings.move_destination_series = request.move_destination_series or None
    settings.media_server_fallback_enabled = request.media_server_fallback_enabled
    settings.default_arr_delete_behavior = request.default_arr_delete_behavior
    settings.add_arr_import_exclusions_on_delete = (
        request.add_arr_import_exclusions_on_delete
    )
    settings.auto_delete_movie_delay_days = request.auto_delete_movie_delay_days
    settings.auto_delete_series_delay_days = request.auto_delete_series_delay_days
    settings.application_url = request.application_url
    settings.playback_movie_min_seconds = request.playback_movie_min_seconds
    settings.playback_episode_min_seconds = request.playback_episode_min_seconds
    settings.favorites_ignore_enabled = request.favorites_ignore_enabled
    settings.favorites_protect_all_users = request.favorites_protect_all_users
    settings.favorites_usernames = request.favorites_usernames
    settings.requester_watch_user_mappings = [
        mapping.model_dump(mode="json")
        for mapping in request.requester_watch_user_mappings
    ]
    settings.requester_watch_ignore_request_date = (
        request.requester_watch_ignore_request_date
    )
    settings.default_allowed_pages = [
        page.value for page in request.default_allowed_pages
    ]
    settings.leaving_soon_enabled = request.leaving_soon_enabled
    settings.leaving_soon_collection_title = current_leaving_soon_title
    if was_leaving_soon_enabled and not settings.leaving_soon_enabled:
        await _cleanup_leaving_soon_collections_on_disable(db, settings)

    # update metadata
    settings.updated_at = datetime.now(UTC)
    settings.updated_by_user_id = admin.id

    db.add(settings)
    await db.commit()
    await db.refresh(settings)
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
    """Get every playback-user name a Seerr requester can be mapped to."""
    if refresh:
        ok, error = await media_watch_snapshot_cache.refresh_snapshot()
        if not ok:
            raise HTTPException(
                status_code=503,
                detail=error or "Failed to refresh watch snapshot",
            )

    # The alias registry knows every provider account, including Tautulli and
    # Tracearr users who never appear in the watch snapshot tables. Watch
    # snapshot keys are unioned in so accounts a provider no longer lists are
    # still selectable.
    alias_rows = (
        await db.execute(
            select(
                WatchUserAlias.observed_service,
                WatchUserAlias.source_service_config_id,
                WatchUserAlias.provider_user_id,
                WatchUserAlias.alias,
                WatchUserAlias.alias_normalized,
            ).distinct()
        )
    ).all()
    snapshot_rows = (
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

    # One row per person, not per recorded string: a single Plex account can be
    # registered under a title, a username, an email, a numeric account id and a
    # Tracearr identity, and listing those separately makes the picker
    # unusable -- and invites mapping the same person twice.
    aliases_by_account: dict[Service, dict[tuple[int, str], set[str]]] = {}
    display_by_normalized: dict[str, str] = {}
    services_by_normalized: dict[str, set[Service]] = {}
    for (
        observed_service,
        config_id,
        provider_user_id,
        alias,
        alias_normalized,
    ) in alias_rows:
        normalized = str(alias_normalized or "").strip().lower()
        if not normalized:
            continue
        aliases_by_account.setdefault(observed_service, {}).setdefault(
            (int(config_id), str(provider_user_id)), set()
        ).add(normalized)
        display_by_normalized.setdefault(normalized, str(alias or "").strip())
        services_by_normalized.setdefault(normalized, set()).add(observed_service)

    for source_service, user_key, user_key_normalized in snapshot_rows:
        normalized = str(user_key_normalized or "").strip().lower()
        if not normalized:
            continue
        display_by_normalized.setdefault(normalized, str(user_key or "").strip())
        services_by_normalized.setdefault(normalized, set()).add(source_service)

    result: list[WatchUserLookupResponse] = []
    grouped: set[str] = set()
    for observed_service, accounts in aliases_by_account.items():
        provider_ids = {user_id.strip().lower() for _, user_id in accounts}
        for person in merge_directory_accounts(accounts):
            label = _pick_watch_user_label(person, provider_ids, display_by_normalized)
            services: set[Service] = {observed_service}
            for alias in person:
                services |= services_by_normalized.get(alias, set())
            grouped |= person
            result.append(
                WatchUserLookupResponse(
                    user_key=display_by_normalized.get(label) or label,
                    user_key_normalized=label,
                    source_services=sorted(services, key=lambda s: str(s.value)),
                    aliases=sorted(
                        display_by_normalized.get(alias) or alias
                        for alias in person
                        if alias != label
                    ),
                )
            )

    # Keys a provider no longer lists still need to be selectable.
    for normalized, display in display_by_normalized.items():
        if normalized in grouped:
            continue
        result.append(
            WatchUserLookupResponse(
                user_key=display or normalized,
                user_key_normalized=normalized,
                source_services=sorted(
                    services_by_normalized.get(normalized, set()),
                    key=lambda s: str(s.value),
                ),
            )
        )

    result.sort(key=lambda item: item.user_key.lower())
    return result


# Long enough that a real word like "beaded" cannot be mistaken for a uuid.
_OPAQUE_ID_MIN_LENGTH = 8
_HEXISH = set("0123456789abcdef-")


def _pick_watch_user_label(
    aliases: frozenset[str],
    provider_ids: set[str],
    _display_by_normalized: dict[str, str],
) -> str:
    """Choose the name a person should be listed under.

    Provider ids, opaque uuids and email addresses identify an account but do
    not name it, so they are the last resort rather than the first.
    """

    def rank(alias: str) -> tuple[int, int, str]:
        is_id = (
            alias in provider_ids
            or alias.isdigit()
            or (
                len(alias) >= _OPAQUE_ID_MIN_LENGTH
                and all(char in _HEXISH for char in alias)
            )
        )
        is_email = "@" in alias
        # A display name with a space reads better than a login handle.
        return (
            2 if is_id else 1 if is_email else 0,
            0 if " " in alias else 1,
            alias,
        )

    return min(aliases, key=rank)


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
    p: PureWindowsPath | PurePosixPath
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
) -> list[dict[str, Any]]:
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
