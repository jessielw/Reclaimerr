from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin, require_page_access
from backend.database import get_db
from backend.database.models import (
    GeneralSettings,
    Movie,
    ReclaimCandidate,
    ReclaimHistory,
    Series,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, PageAccess, Service
from backend.models.storage import (
    StorageInstanceUsage,
    StorageLibraryTotals,
    StorageMount,
    StorageMountSource,
    StorageMountUpdate,
    StorageReclaimable,
    StorageReclaimed,
    StorageResponse,
)
from backend.services.arr_disk import load_arr_disk_space
from backend.services.arr_readd import READD_ACTION
from backend.services.reclaimable import non_reclaiming_candidate_totals

router = APIRouter(prefix="/api", tags=["storage"])


def _override_key(service_type: str, config_id: int, path: str) -> str:
    # Neither capacity nor free space is part of a saved choice: both can change.
    return json.dumps([service_type, config_id, path])


def _select_capacity_mounts(mounts: list[StorageMount]) -> list[StorageMount]:
    """Prefer explicit includes, then count matching capacity/free snapshots once.

    Arr supplies no filesystem identity. Equal snapshots are a heuristic; a user
    can explicitly include independent disks that happen to have equal values.
    Unknown totals/free values never establish an automatic duplicate.
    """
    seen: dict[tuple[int, int], str] = {}
    selected = []
    for mount in sorted(
        mounts, key=lambda item: (item.capacity_mode != "include", item.path)
    ):
        mount.duplicate_of = None
        mount.included_in_capacity = mount.capacity_mode != "exclude"
        if not mount.included_in_capacity:
            continue
        if mount.total_bytes is not None and mount.free_bytes is not None:
            key = (mount.total_bytes, mount.free_bytes)
            if mount.capacity_mode == "auto" and key in seen:
                mount.duplicate_of = seen[key]
                mount.included_in_capacity = False
                continue
            seen.setdefault(key, mount.path)
        selected.append(mount)
    return selected


@router.put("/storage/mounts")
async def update_storage_mounts(
    request: StorageMountUpdate,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Save instance-wide display choices without changing rule disk statistics."""
    settings = (await db.scalars(select(GeneralSettings))).first()
    if settings is None:
        settings = GeneralSettings()
        db.add(settings)
    overrides = dict(settings.storage_mount_overrides)
    for target in request.targets:
        key = _override_key(target.service_type, target.service_config_id, target.path)
        if request.mode == "auto":
            overrides.pop(key, None)
        else:
            overrides[key] = request.mode == "include"
    settings.storage_mount_overrides = overrides
    await db.commit()
    return {"message": "Storage capacity choice saved"}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


async def _instance_names(db: AsyncSession) -> dict[tuple[str, int], str]:
    rows = (
        await db.execute(
            select(
                ServiceConfig.id, ServiceConfig.service_type, ServiceConfig.name
            ).where(ServiceConfig.service_type.in_((Service.RADARR, Service.SONARR)))
        )
    ).all()
    return {
        (service_type.value, config_id): (name or service_type.value.title())
        for config_id, service_type, name in rows
    }


@router.get("/storage", response_model=StorageResponse)
async def get_storage(
    _user: Annotated[User, Depends(require_page_access(PageAccess.STORAGE))],
    db: AsyncSession = Depends(get_db),
) -> StorageResponse:
    """Report disk capacity, library size, and what cleanup has reclaimed.

    Capacity comes from the Arr instances rather than from this container, so
    the numbers match what Radarr and Sonarr themselves see. An instance that
    cannot be reached is reported in ``errors`` instead of failing the page.
    """
    disk = await load_arr_disk_space()
    names = await _instance_names(db)
    settings = (await db.scalars(select(GeneralSettings))).first()
    overrides = settings.storage_mount_overrides if settings else {}

    # The same volume can be reported by several instances. Keying on path and
    # total size keeps genuinely different disks apart when two hosts happen to
    # expose the same container path.
    merged: dict[tuple[str, int | None], StorageMount] = {}
    per_instance: dict[tuple[str, int], StorageInstanceUsage] = {}
    instance_reports: dict[
        tuple[str, int], dict[tuple[str, int | None], StorageMount]
    ] = {}

    for entry in disk.entries:
        path = str(entry.get("path") or "")
        if not path:
            continue
        service_type = str(entry.get("service_type") or "")
        config_id = _as_int(entry.get("service_config_id")) or 0
        # the Arr clients normalize to free_space/total_space, and report a
        # total of 0 when the mount does not expose one
        total = _as_int(entry.get("total_space")) or None
        free = _as_int(entry.get("free_space"))
        used = total - free if total is not None and free is not None else None
        label = entry.get("label")

        source = StorageMountSource(
            service_type=service_type,
            service_config_id=config_id,
            name=names.get((service_type, config_id), service_type.title()),
        )

        key = (path, total)
        mount = merged.get(key)
        if mount is None:
            merged[key] = StorageMount(
                path=path,
                label=str(label) if isinstance(label, str) and label else None,
                total_bytes=total,
                free_bytes=free,
                used_bytes=used,
                sources=[source],
            )
        elif not any(
            existing.service_type == source.service_type
            and existing.service_config_id == source.service_config_id
            for existing in mount.sources
        ):
            mount.sources.append(source)

        instance_key = (service_type, config_id)
        report = StorageMount(
            path=path,
            total_bytes=total,
            free_bytes=free,
            used_bytes=used,
            sources=[source],
        )
        choice = overrides.get(_override_key(service_type, config_id, path))
        if choice is not None:
            report.capacity_mode = "include" if choice else "exclude"
        instance_reports.setdefault(instance_key, {}).setdefault(key, report)
        usage = per_instance.get(instance_key)
        if usage is None:
            usage = StorageInstanceUsage(
                service_type=service_type,
                service_config_id=config_id,
                name=source.name,
                mount_count=0,
                total_bytes=0,
                free_bytes=0,
                used_bytes=0,
            )
            per_instance[instance_key] = usage
    mounts = sorted(merged.values(), key=lambda item: item.path)
    for mount in mounts:
        choices = [
            overrides.get(
                _override_key(source.service_type, source.service_config_id, mount.path)
            )
            for source in mount.sources
        ]
        # A merged row represents one volume. Explicit inclusion takes precedence
        # if previously separate rows have acquired conflicting saved choices.
        if True in choices:
            mount.capacity_mode = "include"
        elif False in choices:
            mount.capacity_mode = "exclude"

    counted = _select_capacity_mounts(mounts)
    capacity_total = sum(mount.total_bytes or 0 for mount in counted)
    capacity_free = sum(mount.free_bytes or 0 for mount in counted)
    capacity_used = sum(mount.used_bytes or 0 for mount in counted)
    capacity_incomplete = any(mount.total_bytes is None for mount in counted)

    for instance_key, usage in per_instance.items():
        selected = _select_capacity_mounts(
            list(instance_reports[instance_key].values())
        )
        usage.mount_count = len(selected)
        usage.total_bytes = sum(mount.total_bytes or 0 for mount in selected)
        usage.free_bytes = sum(mount.free_bytes or 0 for mount in selected)
        usage.used_bytes = sum(mount.used_bytes or 0 for mount in selected)

    library_rows = (
        await db.execute(
            select(
                select(func.count())
                .select_from(Movie)
                .where(Movie.removed_at.is_(None))
                .scalar_subquery()
                .label("movie_count"),
                select(func.coalesce(func.sum(Movie.size), 0))
                .select_from(Movie)
                .where(Movie.removed_at.is_(None))
                .scalar_subquery()
                .label("movie_bytes"),
                select(func.count())
                .select_from(Series)
                .where(Series.removed_at.is_(None))
                .scalar_subquery()
                .label("series_count"),
                select(func.coalesce(func.sum(Series.size), 0))
                .select_from(Series)
                .where(Series.removed_at.is_(None))
                .scalar_subquery()
                .label("series_bytes"),
            )
        )
    ).one()

    libraries = [
        StorageLibraryTotals(
            media_type=MediaType.MOVIE.value,
            item_count=library_rows.movie_count or 0,
            total_bytes=library_rows.movie_bytes or 0,
        ),
        StorageLibraryTotals(
            media_type=MediaType.SERIES.value,
            item_count=library_rows.series_count or 0,
            total_bytes=library_rows.series_bytes or 0,
        ),
    ]

    reclaimable_rows = (
        await db.execute(
            select(
                ReclaimCandidate.media_type,
                func.count().label("candidate_count"),
                func.coalesce(
                    func.sum(ReclaimCandidate.estimated_space_bytes), 0
                ).label("total_bytes"),
            ).group_by(ReclaimCandidate.media_type)
        )
    ).all()
    # candidates whose rule only swaps the quality profile free no space, so
    # they must not be promised here
    non_reclaiming = await non_reclaiming_candidate_totals(db)
    reclaimable = []
    for media_type, candidate_count, total_bytes in reclaimable_rows:
        excluded_count, excluded_bytes = non_reclaiming.get(media_type, (0, 0))
        remaining_count = max(0, (candidate_count or 0) - excluded_count)
        if remaining_count == 0:
            continue
        reclaimable.append(
            StorageReclaimable(
                media_type=media_type.value,
                candidate_count=remaining_count,
                total_bytes=max(0, (total_bytes or 0) - excluded_bytes),
            )
        )

    reclaimed_rows = (
        await db.execute(
            select(
                ReclaimHistory.media_type,
                ReclaimHistory.action,
                func.count().label("item_count"),
                func.coalesce(func.sum(ReclaimHistory.size), 0).label("total_bytes"),
            )
            .where(ReclaimHistory.action != READD_ACTION)
            .group_by(ReclaimHistory.media_type, ReclaimHistory.action)
        )
    ).all()
    reclaimed = [
        StorageReclaimed(
            media_type=media_type.value,
            action=action or "deleted",
            item_count=item_count or 0,
            total_bytes=total_bytes or 0,
        )
        for media_type, action, item_count, total_bytes in reclaimed_rows
    ]

    return StorageResponse(
        mounts=mounts,
        capacity_total_bytes=capacity_total,
        capacity_free_bytes=capacity_free,
        capacity_used_bytes=capacity_used,
        capacity_incomplete=capacity_incomplete,
        instances=sorted(
            per_instance.values(),
            key=lambda item: (item.service_type, item.name),
        ),
        libraries=libraries,
        library_total_bytes=sum(item.total_bytes for item in libraries),
        reclaimable=sorted(reclaimable, key=lambda item: item.media_type),
        reclaimable_total_bytes=sum(item.total_bytes for item in reclaimable),
        reclaimed=sorted(reclaimed, key=lambda item: (item.media_type, item.action)),
        reclaimed_total_bytes=sum(item.total_bytes for item in reclaimed),
        errors=[
            f"{error.service_type.title()} instance {error.service_config_id} "
            f"did not answer: {error.message}"
            for error in disk.errors
        ],
    )
