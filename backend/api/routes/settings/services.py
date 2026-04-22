from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin
from backend.core.encryption import fer_decrypt, fer_encrypt
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.database import get_db
from backend.database.models import ServiceConfig, ServiceMediaLibrary, User
from backend.enums import BackgroundJobType, Service
from backend.jobs import enqueue_background_job
from backend.models.jobs import ServiceToggleJobPayload
from backend.models.settings import (
    LibrarySelectionUpdate,
    ServiceConfigUpdate,
    UpdateMediaLibrariesRequest,
)
from backend.tasks.sync import sync_media_libraries
from backend.types import MEDIA_SERVERS

router = APIRouter(tags=["settings", "services"])


def _mask_api_key(key: str) -> str:
    """Return a masked version of an API key, showing only the last 4 characters."""
    if not key or len(key) <= 4:
        return "****"
    return f"{'*' * (len(key) - 4)}{key[-4:]}"


@router.get("/services")
async def get_service_settings(
    _current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get current service settings."""
    # service configs
    get_service_configs = await db.execute(select(ServiceConfig))
    service_configs = get_service_configs.scalars().all()

    # gather libraries from the main media server
    get_service_libraries = await db.execute(
        select(
            ServiceMediaLibrary.id,
            ServiceMediaLibrary.library_id,
            ServiceMediaLibrary.library_name,
            ServiceMediaLibrary.media_type,
            ServiceMediaLibrary.selected,
        ).order_by(ServiceMediaLibrary.media_type)
    )
    service_libraries = get_service_libraries.all()

    return {
        config.service_type: {
            "enabled": config.enabled,
            "base_url": config.base_url,
            "api_key": _mask_api_key(
                fer_decrypt(config.api_key) if config.api_key else ""
            ),
            "extra_settings": config.extra_settings,
            "is_main": config.is_main if config.service_type in MEDIA_SERVERS else None,
            # libraries only for the designated main media server
            "libraries": [
                {
                    "id": lib_id,
                    "library_id": library_id,
                    "library_name": library_name,
                    "media_type": media_type,
                    "selected": selected,
                }
                for (
                    lib_id,
                    library_id,
                    library_name,
                    media_type,
                    selected,
                ) in service_libraries
            ]
            if config.service_type in MEDIA_SERVERS and config.is_main
            else None,
        }
        for config in service_configs
    }


@router.post("/save/service")
async def set_service_settings(
    data: ServiceConfigUpdate,
    _current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set service settings for a given service."""
    # if the client omitted the api_key (unchanged masked field), resolve the
    # existing key from the database so we don't overwrite it with garbage
    resolved_api_key = data.api_key
    if not resolved_api_key:
        existing = await db.execute(
            select(ServiceConfig).where(ServiceConfig.service_type == data.service_type)
        )
        existing_config = existing.scalar_one_or_none()
        if not existing_config:
            raise HTTPException(
                status_code=400,
                detail="API key is required when configuring a service for the first time",
            )
        resolved_api_key = fer_decrypt(existing_config.api_key)

    # test service settings before saving
    success, error_msg = await service_manager.test_service(
        data.service_type, data.base_url, resolved_api_key
    )
    if not success:
        raise HTTPException(status_code=400, detail=error_msg)

    # detect if the main server is switching before we write the new config
    main_switched = False
    if data.is_main and data.service_type in MEDIA_SERVERS:
        current_main_result = await db.execute(
            select(ServiceConfig).where(
                ServiceConfig.service_type.in_(MEDIA_SERVERS),
                ServiceConfig.is_main.is_(True),
            )
        )
        current_main = current_main_result.scalar_one_or_none()
        main_switched = (
            current_main is not None and current_main.service_type != data.service_type
        )

    # determine what sync action (if any) to signal the frontend
    sync_action: str | None = None
    if data.is_main and data.enabled and data.service_type in MEDIA_SERVERS:
        sync_action = "resync" if main_switched else "sync"

    # continue to upsert settings
    await _upsert_service_config(
        db,
        ServiceConfigUpdate(
            service_type=data.service_type,
            base_url=data.base_url,
            api_key=resolved_api_key,
            enabled=data.enabled,
            is_main=data.is_main,
            extra_settings=data.extra_settings,
        ),
    )

    queued_job = await enqueue_background_job(
        job_type=BackgroundJobType.SERVICE_TOGGLE,
        payload=ServiceToggleJobPayload(
            service_type=data.service_type,
            base_url=data.base_url,
            api_key=resolved_api_key,
            enabled=data.enabled,
            is_main=data.is_main,
            trigger_resync=main_switched,
        ).model_dump(mode="json"),
        dedupe_key=f"service-toggle-{data.service_type}",
        replace_pending=True,
    )
    if queued_job is None:
        LOG.error(
            f"Failed to enqueue background job for {data.service_type} service toggle"
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to queue service update job",
        )
    LOG.info(
        f"Queued background job {queued_job.id} for {data.service_type} service toggle"
    )

    if main_switched:
        LOG.info(
            f"Main media server switched to {data.service_type} - triggering full resync"
        )

    # update selected toggle for libraries
    if data.service_type in MEDIA_SERVERS and data.libraries:
        await _upsert_service_libraries(db, data.libraries)

    return {
        "message": f"{data.service_type.title()} settings updated",
        "sync_action": sync_action,
        "data": {
            "service_type": data.service_type,
            "base_url": data.base_url,
            "api_key": _mask_api_key(resolved_api_key),
            "enabled": data.enabled,
            "is_main": data.is_main,
            "extra_settings": data.extra_settings,
        },
    }


async def _upsert_service_config(
    db: AsyncSession, data: ServiceConfigUpdate
) -> ServiceConfigUpdate:
    """Upsert service configuration into the database."""
    LOG.info(f"Updating config for {data.service_type}")

    if data.api_key is None:
        raise ValueError(
            "api_key must be resolved before calling _upsert_service_config"
        )

    # if this server is being made main, clear is_main from all other media servers first
    if data.is_main:
        await db.execute(
            sql_update(ServiceConfig)
            .where(
                ServiceConfig.service_type != data.service_type,
                ServiceConfig.service_type.in_(MEDIA_SERVERS),
            )
            .values(is_main=False)
        )

    # upsert into database
    insert_statement = sqlite_insert(ServiceConfig).values(
        service_type=data.service_type,
        base_url=data.base_url,
        api_key=fer_encrypt(data.api_key),
        enabled=data.enabled,
        is_main=data.is_main,
        extra_settings=data.extra_settings,
    )
    upsert_statement = insert_statement.on_conflict_do_update(
        index_elements=["service_type"],
        set_={
            "base_url": data.base_url,
            "api_key": fer_encrypt(data.api_key),
            "enabled": data.enabled,
            "is_main": data.is_main,
            "extra_settings": data.extra_settings,
        },
    )
    await db.execute(upsert_statement)
    await db.commit()
    return data


async def _upsert_service_libraries(
    db: AsyncSession,
    libraries: list[dict],
) -> None:
    """Update library selections by ID."""
    LOG.info("Updating library selections")

    for lib in libraries:
        # update selected status by library ID
        result = await db.execute(
            select(ServiceMediaLibrary).where(ServiceMediaLibrary.id == lib["id"])
        )
        library = result.scalar_one_or_none()
        if library:
            library.selected = lib["selected"]

    await db.commit()


@router.post("/test/service")
async def test_service_settings(
    data: ServiceConfigUpdate,
    _current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Test service settings for a given service."""
    resolved_api_key = data.api_key
    if not resolved_api_key:
        existing = await db.execute(
            select(ServiceConfig).where(ServiceConfig.service_type == data.service_type)
        )
        existing_config = existing.scalar_one_or_none()
        if not existing_config:
            raise HTTPException(
                status_code=400,
                detail="API key is required to test a service that has not been configured yet",
            )
        resolved_api_key = fer_decrypt(existing_config.api_key)

    success, error_msg = await service_manager.test_service(
        data.service_type, data.base_url, resolved_api_key
    )
    if not success:
        raise HTTPException(status_code=400, detail=error_msg)
    return {
        "message": f"{data.service_type} settings tested successfully",
        "data": {
            "service_type": data.service_type,
            "base_url": data.base_url,
            "enabled": data.enabled,
        },
    }


@router.post("/sync/libraries")
async def update_service_libraries(
    service_type: UpdateMediaLibrariesRequest,
    _current_user: Annotated[User, Depends(require_admin)],
) -> dict[str, Any]:
    """Sync library selections for a given service."""
    if not service_type.service_type or service_type.service_type not in (
        Service.JELLYFIN,
        Service.EMBY,
        Service.PLEX,
    ):
        raise HTTPException(
            status_code=400,
            detail="Library selection is only supported for Jellyfin, Emby and Plex",
        )

    # update libraries from the main server
    return await sync_media_libraries()


@router.put("/libraries")
async def update_library_selections(
    data: list[LibrarySelectionUpdate],
    _current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update library selected state by ID."""
    await _upsert_service_libraries(
        db, [{"id": item.id, "selected": item.selected} for item in data]
    )
    return {"message": "Library selections updated"}
