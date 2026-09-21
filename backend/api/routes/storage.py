from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_page_access
from backend.database import get_db
from backend.database.models import (
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
    StorageReclaimable,
    StorageReclaimed,
    StorageResponse,
)
from backend.services.arr_disk import load_arr_disk_space
from backend.services.reclaimable import non_reclaiming_candidate_totals

router = APIRouter(prefix="/api", tags=["storage"])


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

    # The same volume can be reported by several instances. Keying on path and
    # total size keeps genuinely different disks apart when two hosts happen to
    # expose the same container path.
    merged: dict[tuple[str, int | None], StorageMount] = {}
    per_instance: dict[tuple[str, int], StorageInstanceUsage] = {}
    counted_for_instance: set[tuple[str, int, str]] = set()

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
        # one instance can list the same path twice; count it once
        dedupe = (service_type, config_id, path)
        if dedupe not in counted_for_instance:
            counted_for_instance.add(dedupe)
            usage.mount_count += 1
            usage.total_bytes += total or 0
            usage.free_bytes += free or 0
            usage.used_bytes += used or 0

    mounts = sorted(merged.values(), key=lambda item: item.path)
    capacity_total = sum(mount.total_bytes or 0 for mount in mounts)
    capacity_free = sum(mount.free_bytes or 0 for mount in mounts)
    capacity_used = sum(mount.used_bytes or 0 for mount in mounts)
    capacity_incomplete = any(mount.total_bytes is None for mount in mounts)

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
            ).group_by(ReclaimHistory.media_type, ReclaimHistory.action)
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
