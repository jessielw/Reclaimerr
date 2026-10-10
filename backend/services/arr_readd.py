"""Add a deleted movie or series back to Radarr or Sonarr from its History row.

Only whole titles are re-added. A History row for a version, season, or
episode usually leaves the title in the Arr, and that case is reported as
"already added" rather than restored piece by piece.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.utils.misc import as_int
from backend.database.models import ReclaimHistory, Series, ServiceConfig
from backend.enums import MediaType, Service
from backend.models.media import (
    ReAddInstance,
    ReAddOptions,
    ReAddQualityProfile,
    ReAddRequest,
    ReAddResponse,
)

READD_ACTION = "re-added"


class ReAddError(ValueError):
    """A re-add that can't go ahead, with a message meant for the admin."""


def _service_for(row: ReclaimHistory) -> Service:
    return Service.RADARR if row.media_type is MediaType.MOVIE else Service.SONARR


def _arr_label(service: Service) -> str:
    return "Radarr" if service is Service.RADARR else "Sonarr"


def _clients(service: Service) -> dict[int, Any]:
    return (
        service_manager.radarr_clients()
        if service is Service.RADARR
        else service_manager.sonarr_clients()
    )


def _lookup_title(lookup: Mapping[str, object]) -> str:
    title = str(lookup.get("title") or "Unknown title")
    year = as_int(lookup.get("year"))
    return f"{title} ({year})" if year else title


def _already_added(lookup: Mapping[str, object]) -> bool:
    return (as_int(lookup.get("id")) or 0) > 0


async def _arr_id(db: AsyncSession, row: ReclaimHistory) -> int:
    """The ID the Arr looks the title up by: TMDB for Radarr, TVDB for Sonarr."""
    if row.action != "deleted":
        raise ReAddError("Only deleted titles can be re-added.")
    if row.tmdb_id is None:
        raise ReAddError("This record has no TMDB ID to look the title up by.")
    if row.media_type is MediaType.MOVIE:
        return row.tmdb_id
    tvdb_id = as_int(
        await db.scalar(select(Series.tvdb_id).where(Series.tmdb_id == row.tmdb_id))
    )
    if not tvdb_id:
        raise ReAddError(
            "Reclaimerr has no TVDB ID for this series, so Sonarr can't look it "
            "up. Add it from Sonarr."
        )
    return tvdb_id


async def _lookup(
    client: Any, service: Service, arr_id: int
) -> Mapping[str, object] | None:
    if service is Service.RADARR:
        return await client.lookup_movie(arr_id)
    return await client.lookup_series(arr_id)


async def _instance_names(db: AsyncSession, service: Service) -> dict[int, str]:
    rows = await db.execute(
        select(ServiceConfig.id, ServiceConfig.name).where(
            ServiceConfig.service_type == service
        )
    )
    return {config_id: name or _arr_label(service) for config_id, name in rows.all()}


async def get_readd_options(db: AsyncSession, row: ReclaimHistory) -> ReAddOptions:
    """What the re-add dialog needs: each instance's folders, profiles, and
    whether the title is already there."""
    service = _service_for(row)
    options = ReAddOptions(
        history_id=row.id,
        media_type=row.media_type.value,
        service="radarr" if service is Service.RADARR else "sonarr",
    )
    try:
        arr_id = await _arr_id(db, row)
    except ReAddError as exc:
        options.unavailable_reason = str(exc)
        return options

    clients = _clients(service)
    if not clients:
        options.unavailable_reason = f"No {_arr_label(service)} instance is configured."
        return options

    names = await _instance_names(db, service)

    async def describe(config_id: int, client: Any) -> ReAddInstance:
        instance = ReAddInstance(
            service_config_id=config_id,
            service_name=names.get(config_id) or _arr_label(service),
        )
        try:
            lookup, folders, profiles = await asyncio.gather(
                _lookup(client, service, arr_id),
                client.get_root_folders(),
                client.get_quality_profiles(),
            )
        except Exception as exc:
            LOG.warning(f"Re-add lookup failed on {instance.service_name}: {exc}")
            instance.error = str(exc)
            return instance
        if lookup is None:
            instance.error = f"{_arr_label(service)} could not find this title."
            return instance
        if options.title is None:
            options.title = _lookup_title(lookup)
        instance.already_added = _already_added(lookup)
        instance.root_folders = folders
        instance.quality_profiles = [
            ReAddQualityProfile(id=profile.id, name=profile.name)
            for profile in profiles
        ]
        return instance

    options.instances = list(
        await asyncio.gather(
            *(
                describe(config_id, client)
                for config_id, client in sorted(clients.items())
            )
        )
    )
    return options


async def readd_title(
    db: AsyncSession,
    row: ReclaimHistory,
    request: ReAddRequest,
    *,
    username: str,
) -> ReAddResponse:
    """Add the title, clear its import exclusion, and record it in History."""
    service = _service_for(row)
    label = _arr_label(service)
    arr_id = await _arr_id(db, row)
    client = _clients(service).get(request.service_config_id)
    if client is None:
        raise ReAddError(f"That {label} instance is not available.")

    lookup = await _lookup(client, service, arr_id)
    if lookup is None:
        raise ReAddError(f"{label} could not find this title.")
    title = _lookup_title(lookup)
    if _already_added(lookup):
        raise ReAddError(f"{title} is already in {label}.")

    add = client.add_movie if service is Service.RADARR else client.add_series
    await add(
        lookup,
        root_folder_path=request.root_folder_path,
        quality_profile_id=request.quality_profile_id,
        search=request.search,
    )
    LOG.info(
        f"{username} re-added '{title}' to {label} config {request.service_config_id}"
    )

    message = f"{title} was added to {label}."
    exclusion_removed = False
    try:
        exclusion_removed = await client.remove_import_exclusion(arr_id)
    except Exception as exc:
        # the title is already added; the exclusion only matters to import lists
        LOG.warning(
            f"Could not remove the {label} import exclusion for '{title}': {exc}"
        )
        message += f" Its import exclusion could not be removed: {exc}"

    db.add(
        ReclaimHistory(
            approved_by=username,
            media_type=row.media_type,
            tmdb_id=row.tmdb_id,
            name=title,
            action=READD_ACTION,
        )
    )
    await db.commit()
    return ReAddResponse(
        message=message, title=title, exclusion_removed=exclusion_removed
    )
