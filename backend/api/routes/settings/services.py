from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin
from backend.core.encryption import fer_decrypt, fer_encrypt
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.database import get_db
from backend.database.models import ServiceConfig, ServiceMediaLibrary, User
from backend.enums import Service
from backend.models.settings import ServiceConfigUpdate, UpdateMediaLibrariesRequest
from backend.tasks.sync import sync_service_libraries

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

    # gather libraries from Jellyfin and/or Plex
    MEDIA_SERVICES = (Service.JELLYFIN, Service.PLEX)
    get_service_libraries = await db.execute(
        select(
            ServiceMediaLibrary.id,
            ServiceMediaLibrary.service_type,
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
            # sort libraries for Plex and Jellyfin only
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
                    service_type,
                    library_id,
                    library_name,
                    media_type,
                    selected,
                ) in service_libraries
                if service_type == config.service_type
            ]
            if config.service_type in MEDIA_SERVICES  # only include for these services
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

    # continue to upsert settings
    await _upsert_service_config(
        db,
        ServiceConfigUpdate(
            service_type=data.service_type,
            base_url=data.base_url,
            api_key=resolved_api_key,
            enabled=data.enabled,
        ),
    )

    # clear and enable/disable clients (trigger this to start in background)
    asyncio.create_task(
        _toggle_service(
            ServiceConfigUpdate(
                service_type=data.service_type,
                base_url=data.base_url,
                api_key=resolved_api_key,
                enabled=data.enabled,
            ),
        )
    )

    # update selected toggle for libraries
    if data.service_type in (Service.JELLYFIN, Service.PLEX) and data.libraries:
        await _upsert_service_libraries(db, data.service_type, data.libraries)

    return {
        "message": (
            f"{data.service_type.title()} settings updated "
            f"{'' if data.enabled else 'and client disabled'}"
        ),
        "data": {
            "service_type": data.service_type,
            "base_url": data.base_url,
            "api_key": _mask_api_key(resolved_api_key),
            "enabled": data.enabled,
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

    # upsert into database
    insert_statement = sqlite_insert(ServiceConfig).values(
        service_type=data.service_type,
        base_url=data.base_url,
        api_key=fer_encrypt(data.api_key),
        enabled=data.enabled,
    )
    upsert_statement = insert_statement.on_conflict_do_update(
        index_elements=["service_type"],
        set_={
            "base_url": data.base_url,
            "api_key": fer_encrypt(data.api_key),
            "enabled": data.enabled,
        },
    )
    await db.execute(upsert_statement)
    await db.commit()
    return data


async def _toggle_service(data: ServiceConfigUpdate) -> None:
    if data.api_key is None:
        raise ValueError("api_key must be resolved before calling _toggle_service")
    if data.service_type is Service.JELLYFIN:
        await service_manager.clear_jellyfin()
        if data.enabled:
            await service_manager.initialize_jellyfin(data.base_url, data.api_key)
    elif data.service_type is Service.PLEX:
        await service_manager.clear_plex()
        if data.enabled:
            await service_manager.initialize_plex(data.base_url, data.api_key)
    elif data.service_type is Service.RADARR:
        await service_manager.clear_radarr()
        if data.enabled:
            await service_manager.initialize_radarr(data.base_url, data.api_key)
    elif data.service_type is Service.SONARR:
        await service_manager.clear_sonarr()
        if data.enabled:
            await service_manager.initialize_sonarr(data.base_url, data.api_key)
    elif data.service_type is Service.SEERR:
        await service_manager.clear_seerr()
        if data.enabled:
            await service_manager.initialize_seerr(data.base_url, data.api_key)

    # log the status after toggling
    statuses = await service_manager.get_status()
    LOG.debug(f"Service statuses: {statuses}")


async def _upsert_service_libraries(
    db: AsyncSession,
    service_type: Service,
    libraries: list[dict],
) -> None:
    """Update library selections by ID."""
    LOG.info(f"Updating libraries for {service_type}")

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
) -> dict[Literal[Service.PLEX, Service.JELLYFIN], list[dict[str, Any]]]:
    """Sync library selections for a given service."""
    if not service_type.service_type or service_type.service_type not in (
        Service.JELLYFIN,
        Service.PLEX,
    ):
        raise HTTPException(
            status_code=400,
            detail="Library selection is only supported for Jellyfin and Plex",
        )

    # update libraries for the service
    updated_libraries = await sync_service_libraries(service_type.service_type)

    return updated_libraries
