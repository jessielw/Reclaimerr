from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin
from backend.core.encryption import fer_decrypt, fer_encrypt
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.database import get_db
from backend.database.models import (
    BackgroundJob,
    ExternalRatingsIngestState,
    GeneralSettings,
    Movie,
    MovieArrRef,
    ReclaimRule,
    Series,
    SeriesArrRef,
    ServiceConfig,
    ServiceMediaLibrary,
    User,
)
from backend.enums import (
    BackgroundJobPriority,
    BackgroundJobStatus,
    BackgroundJobType,
    Service,
)
from backend.jobs.queue import enqueue_background_job
from backend.models.jobs import ServiceToggleJobPayload
from backend.models.settings import (
    LibrarySelectionUpdate,
    ServiceConfigUpdate,
    UpdateMediaLibrariesRequest,
)
from backend.tasks.external_ratings import (
    DEFAULT_MDBLIST_REQUEST_LIMIT,
    DEFAULT_OMDB_REQUEST_LIMIT,
)
from backend.tasks.sync import sync_media_libraries
from backend.user_types import MEDIA_SERVERS

router = APIRouter(tags=["settings", "services"])

ARR_SERVICES = {Service.RADARR, Service.SONARR}
METADATA_PROVIDER_SERVICES = (Service.MDBLIST, Service.OMDB)
METADATA_PROVIDER_DEFAULT_REQUEST_LIMITS = {
    Service.MDBLIST: DEFAULT_MDBLIST_REQUEST_LIMIT,
    Service.OMDB: DEFAULT_OMDB_REQUEST_LIMIT,
}
METADATA_PROVIDER_DEFAULT_REQUEST_DELAYS = {
    Service.MDBLIST: 1.0,
    Service.OMDB: 0.25,
}


def _default_service_name(service_type: Service) -> str:
    if service_type is Service.MDBLIST:
        return "MDBList"
    if service_type is Service.OMDB:
        return "OMDb"
    return service_type.title()


def _mask_api_key(key: str) -> str:
    """Return a masked version of an API key, showing only the last 4 characters."""
    if not key or len(key) <= 4:
        return "****"
    return f"{'*' * (len(key) - 4)}{key[-4:]}"


def _parse_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coverage_payload(covered: int, total: int) -> dict[str, int | float]:
    return {
        "covered": covered,
        "total": total,
        "percent": round((covered / total) * 100, 1) if total else 0,
    }


async def _count_active_media(db: AsyncSession) -> tuple[int, int]:
    movie_count = await db.scalar(
        select(func.count()).select_from(Movie).where(Movie.removed_at.is_(None))
    )
    series_count = await db.scalar(
        select(func.count()).select_from(Series).where(Series.removed_at.is_(None))
    )
    return int(movie_count or 0), int(series_count or 0)


async def _count_provider_coverage(
    db: AsyncSession, provider: Service
) -> tuple[int, int]:
    token = f"%{provider.value}%"
    movie_count = await db.scalar(
        select(func.count())
        .select_from(Movie)
        .where(
            Movie.removed_at.is_(None),
            func.lower(Movie.external_ratings_source).like(token),
        )
    )
    series_count = await db.scalar(
        select(func.count())
        .select_from(Series)
        .where(
            Series.removed_at.is_(None),
            func.lower(Series.external_ratings_source).like(token),
        )
    )
    return int(movie_count or 0), int(series_count or 0)


def _provider_request_limit(config: ServiceConfig | None, provider: Service) -> int:
    default = METADATA_PROVIDER_DEFAULT_REQUEST_LIMITS[provider]
    if config is None:
        return default
    raw = (config.extra_settings or {}).get("request_limit", default)
    value = _parse_optional_int(raw)
    if value is None:
        return default
    return max(1, min(value, 5000))


def _provider_request_delay(config: ServiceConfig | None, provider: Service) -> float:
    default = METADATA_PROVIDER_DEFAULT_REQUEST_DELAYS[provider]
    if config is None:
        return default
    raw = (config.extra_settings or {}).get("request_delay_seconds", default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(value, 10.0))


async def _metadata_provider_status_payload(db: AsyncSession) -> dict[str, Any]:
    configs = (
        await db.execute(
            select(ServiceConfig).where(
                ServiceConfig.service_type.in_(METADATA_PROVIDER_SERVICES)
            )
        )
    ).scalars()
    configs_by_service = {config.service_type: config for config in configs}

    state = (
        (
            await db.execute(
                select(ExternalRatingsIngestState).order_by(
                    ExternalRatingsIngestState.id.asc()
                )
            )
        )
        .scalars()
        .first()
    )
    provider_summary = state.provider_summary if state else None
    if not isinstance(provider_summary, dict):
        provider_summary = {}

    total_movies, total_series = await _count_active_media(db)
    total_media = total_movies + total_series
    providers: list[dict[str, Any]] = []
    for provider in METADATA_PROVIDER_SERVICES:
        config = configs_by_service.get(provider)
        provider_run_summary = provider_summary.get(provider.value)
        if not isinstance(provider_run_summary, dict):
            provider_run_summary = {}
        covered_movies, covered_series = await _count_provider_coverage(db, provider)
        covered_total = covered_movies + covered_series
        providers.append(
            {
                "service_type": provider.value,
                "name": _default_service_name(provider),
                "configured": config is not None and bool(config.api_key),
                "enabled": bool(config.enabled) if config is not None else False,
                "request_limit": _provider_request_limit(config, provider),
                "request_delay_seconds": _provider_request_delay(config, provider),
                "last_run_requests": _parse_optional_int(
                    provider_run_summary.get("requests_used")
                ),
                "last_run_request_limit": _parse_optional_int(
                    provider_run_summary.get("request_limit")
                ),
                "disabled_reason": provider_run_summary.get("disabled_reason")
                if isinstance(provider_run_summary.get("disabled_reason"), str)
                else None,
                "last_checked_at": provider_run_summary.get("last_checked_at")
                if isinstance(provider_run_summary.get("last_checked_at"), str)
                else None,
                "last_successful_refresh_at": provider_run_summary.get(
                    "last_successful_refresh_at"
                )
                if isinstance(
                    provider_run_summary.get("last_successful_refresh_at"), str
                )
                else None,
                "last_error": provider_run_summary.get("last_error")
                if isinstance(provider_run_summary.get("last_error"), str)
                else None,
                "coverage": {
                    "movies": _coverage_payload(covered_movies, total_movies),
                    "series": _coverage_payload(covered_series, total_series),
                    "total": _coverage_payload(covered_total, total_media),
                },
            }
        )

    return {
        "providers": providers,
        "last_checked_at": to_utc_isoformat(state.last_checked_at)
        if state and state.last_checked_at
        else None,
        "last_successful_refresh_at": to_utc_isoformat(state.last_successful_refresh_at)
        if state and state.last_successful_refresh_at
        else None,
        "last_error": state.last_error if state else None,
    }


@router.get("/services")
async def get_service_settings(
    _current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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

    response: dict[str, Any] = {}
    for config in service_configs:
        payload = _service_config_payload(config)
        payload["libraries"] = (
            [
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
            else None
        )

        key = config.service_type
        if key in ARR_SERVICES:
            bucket = response.setdefault(key, {"instances": []})
            bucket["instances"].append(payload)
            if "id" not in bucket:
                bucket.update(payload)
        else:
            response[key] = payload
    return response


@router.get("/metadata-providers/status")
async def get_metadata_provider_status(
    _current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get usage and media coverage status for metadata providers."""
    return await _metadata_provider_status_payload(db)


def _service_config_payload(config: ServiceConfig) -> dict[str, Any]:
    """Prepare service configuration payload, masking API key and including main server libraries."""
    return {
        "id": config.id,
        "name": config.name or _default_service_name(config.service_type),
        "service_type": config.service_type,
        "enabled": config.enabled,
        "base_url": config.base_url,
        "api_key": _mask_api_key(fer_decrypt(config.api_key) if config.api_key else ""),
        "extra_settings": config.extra_settings,
        "is_main": config.is_main if config.service_type in MEDIA_SERVERS else None,
    }


@router.post("/save/service")
async def set_service_settings(
    data: ServiceConfigUpdate,
    _current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Set service settings for a given service."""
    service_name = data.name or _default_service_name(data.service_type)
    # if the client omitted the api_key (unchanged masked field), resolve the
    # existing key from the database so we don't overwrite it with garbage
    resolved_api_key = data.api_key
    if not resolved_api_key:
        existing = await _find_existing_service_config(db, data, service_name)
        existing_config = existing.scalar_one_or_none()
        if not existing_config:
            raise HTTPException(
                status_code=400,
                detail="API key is required when configuring a service for the first time",
            )
        resolved_api_key = fer_decrypt(existing_config.api_key)

    existing_result = await _find_existing_service_config(db, data, service_name)
    existing_config = existing_result.scalar_one_or_none()
    if (
        not data.enabled
        and data.service_type in MEDIA_SERVERS
        and ((existing_config and existing_config.is_main) or data.is_main)
    ):
        raise HTTPException(
            status_code=409,
            detail="Cannot disable the active main media server. Assign a different main server first.",
        )

    # Enabling requires a verified connection. Disabling is a local configuration
    # operation and must remain available while the external service is offline.
    if data.enabled:
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
            id=data.id,
            name=service_name,
            service_type=data.service_type,
            base_url=data.base_url,
            api_key=resolved_api_key,
            enabled=data.enabled,
            is_main=data.is_main,
            extra_settings=data.extra_settings,
        ),
    )
    saved_config_result = await _find_existing_service_config(db, data, service_name)
    saved_config = saved_config_result.scalar_one()

    queued_job = await enqueue_background_job(
        job_type=BackgroundJobType.SERVICE_TOGGLE,
        payload=ServiceToggleJobPayload(
            service_config_id=saved_config.id,
            name=service_name,
            service_type=data.service_type,
            base_url=data.base_url,
            api_key=resolved_api_key,
            enabled=data.enabled,
            is_main=data.is_main,
            extra_settings=data.extra_settings,
            trigger_resync=main_switched,
        ).model_dump(mode="json"),
        dedupe_key=f"service-toggle-{saved_config.id}",
        replace_pending=True,
        priority=BackgroundJobPriority.HIGH,
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
            "id": saved_config.id,
            "name": service_name,
            "service_type": data.service_type,
            "base_url": data.base_url,
            "api_key": _mask_api_key(resolved_api_key),
            "enabled": data.enabled,
            "is_main": data.is_main,
            "extra_settings": data.extra_settings,
        },
    }


@router.delete("/service/{service_config_id}")
async def delete_service_settings(
    service_config_id: int,
    _current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Deletes a service configuration by ID."""
    result = await db.execute(
        select(ServiceConfig).where(ServiceConfig.id == service_config_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Service configuration not found")

    if config.service_type in MEDIA_SERVERS and config.is_main:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the active main media server. Assign a different main server first.",
        )

    service_type = config.service_type
    config_id = config.id
    affected_rules: list[dict[str, Any]] = []
    removed_path_mappings = 0

    if service_type is Service.RADARR:
        await db.execute(
            sql_delete(MovieArrRef).where(MovieArrRef.service_config_id == config_id)
        )
    elif service_type is Service.SONARR:
        await db.execute(
            sql_delete(SeriesArrRef).where(SeriesArrRef.service_config_id == config_id)
        )

    if service_type in ARR_SERVICES:
        action_key = (
            "radarr_service_config_id"
            if service_type is Service.RADARR
            else "sonarr_service_config_id"
        )
        rules = (await db.execute(select(ReclaimRule))).scalars().all()
        for rule in rules:
            action = dict(rule.action or {})
            if action.get(action_key) != config_id:
                continue
            action[action_key] = None
            rule.action = action
            rule.enabled = False
            affected_rules.append({"id": rule.id, "name": rule.name})

    settings = (await db.execute(select(GeneralSettings))).scalars().first()
    if settings is not None:
        current_mappings = list(settings.path_mappings or [])
        retained_mappings = [
            mapping
            for mapping in current_mappings
            if mapping.get("service_config_id") != config_id
        ]
        removed_path_mappings = len(current_mappings) - len(retained_mappings)
        if removed_path_mappings:
            settings.path_mappings = retained_mappings

    await db.execute(
        sql_update(BackgroundJob)
        .where(
            BackgroundJob.dedupe_key == f"service-toggle-{config_id}",
            BackgroundJob.status == BackgroundJobStatus.PENDING,
        )
        .values(
            status=BackgroundJobStatus.CANCELED,
            completed_at=datetime.now(UTC),
            error_message="Service configuration deleted",
        )
    )
    await db.execute(sql_delete(ServiceConfig).where(ServiceConfig.id == config_id))
    await db.commit()
    try:
        from backend.core.service_runtime import clear_deleted_service_runtime

        await clear_deleted_service_runtime(service_type, config_id)
    except Exception as e:
        LOG.warning(
            f"Service config {config_id} was deleted, but runtime cleanup failed: {e}"
        )

    return {
        "message": f"{service_type.value.title()} configuration deleted",
        "data": {
            "id": config_id,
            "service_type": service_type.value,
            "deleted": True,
            "affected_rules": affected_rules,
            "removed_path_mappings": removed_path_mappings,
        },
    }


async def _find_existing_service_config(
    db: AsyncSession, data: ServiceConfigUpdate, service_name: str
) -> Any:
    """Find existing service configuration by ID or service type/name."""
    if data.id is not None:
        return await db.execute(
            select(ServiceConfig).where(ServiceConfig.id == data.id)
        )
    if data.service_type in ARR_SERVICES:
        return await db.execute(
            select(ServiceConfig).where(
                ServiceConfig.service_type == data.service_type,
                ServiceConfig.name == service_name,
            )
        )
    return await db.execute(
        select(ServiceConfig).where(ServiceConfig.service_type == data.service_type)
    )


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

    service_name = data.name or _default_service_name(data.service_type)
    values: dict[str, Any] = dict(
        service_type=data.service_type,
        name=service_name,
        base_url=data.base_url,
        api_key=fer_encrypt(data.api_key),
        enabled=data.enabled,
        is_main=data.is_main,
        extra_settings=data.extra_settings,
    )

    if data.id is not None:
        await db.execute(
            sql_update(ServiceConfig)
            .where(ServiceConfig.id == data.id)
            .values(**values)
        )
    else:
        insert_statement = sqlite_insert(ServiceConfig).values(**values)
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=["service_type", "name"],
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
    libraries: list[dict[str, Any]],
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
) -> dict[str, Any]:
    """Test service settings for a given service."""
    service_name = data.name or _default_service_name(data.service_type)
    resolved_api_key = data.api_key
    if not resolved_api_key:
        existing = await _find_existing_service_config(db, data, service_name)
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
) -> dict[str, str]:
    """Update library selected state by ID."""
    await _upsert_service_libraries(
        db, [{"id": item.id, "selected": item.selected} for item in data]
    )
    return {"message": "Library selections updated"}
