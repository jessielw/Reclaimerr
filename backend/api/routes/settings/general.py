from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin
from backend.core.utils.filesystem import normalize_fpath
from backend.database import get_db
from backend.database.models import (
    GeneralSettings,
    MovieVersion,
    SeriesServiceRef,
    ServiceConfig,
    User,
)
from backend.enums import MediaType
from backend.models.post_action_webhooks import PostActionWebhookEvent
from backend.models.settings import (
    GeneralSettingsResponse,
    PostActionWebhookTestRequest,
    PostActionWebhookTestResponse,
)
from backend.services.post_action_webhooks import (
    invalidate_webhook_config_cache,
    send_post_action_webhook,
)

router = APIRouter(tags=["settings", "general"])


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

    # update metadata
    settings.updated_at = datetime.now(UTC)
    settings.updated_by_user_id = admin.id

    db.add(settings)
    await db.commit()
    await db.refresh(settings)
    invalidate_webhook_config_cache()
    return GeneralSettingsResponse.model_validate(settings)


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
