from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import has_permission, require_admin, require_page_access
from backend.core.task_runtime import is_task_active, request_task_run
from backend.database import get_db
from backend.database.models import (
    DuplicateIgnore,
    GeneralSettings,
    UpgradeLeftover,
    User,
)
from backend.enums import MediaType, PageAccess, Permission, Task, UserRole
from backend.jobs.duplicate_file_ops import queue_duplicate_delete_job
from backend.models.duplicates import (
    DuplicateDeleteRequest,
    DuplicateFileResponse,
    DuplicateGroupResponse,
    DuplicateIgnoreRequest,
    DuplicateSettings,
    DuplicateSummary,
    KeeperPriorityEntry,
    LeftoverDeleteRequest,
    LeftoverIgnoreRequest,
    LeftoverScanInfo,
    LeftoverSummary,
    PaginatedDuplicatesResponse,
    PaginatedLeftoversResponse,
    UpgradeLeftoverResponse,
)
from backend.models.jobs import DuplicateDeleteJobItem, LeftoverDeleteJobItem
from backend.services.duplicates import (
    DuplicateActionError,
    DuplicateGroup,
    keeper_priority_payload,
    load_duplicate_groups,
    load_group,
    load_keeper_priority,
    normalize_keeper_priority,
    plan_duplicate_delete,
)
from backend.services.upgrade_leftovers import (
    LeftoverView,
    load_leftovers,
    load_scan_summary,
)

router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])


def _require_manage_reclaim(user: User) -> None:
    if user.role is not UserRole.ADMIN and not has_permission(
        user, Permission.MANAGE_RECLAIM
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managing duplicates requires the Manage Reclaim permission",
        )


def _group_label(group: DuplicateGroup) -> str:
    if group.media_type is MediaType.MOVIE:
        return f"{group.title} ({group.year})" if group.year else group.title
    return (
        f"{group.title} S{group.season_number or 0:02d}E{group.episode_number or 0:02d}"
    )


def _serialize(group: DuplicateGroup) -> DuplicateGroupResponse:
    return DuplicateGroupResponse(
        key=group.key,
        media_type=group.media_type,
        item_id=group.item_id,
        title=group.title,
        year=group.year,
        poster_url=group.poster_url,
        series_id=group.series_id,
        season_number=group.season_number,
        episode_number=group.episode_number,
        episode_name=group.episode_name,
        files=[
            DuplicateFileResponse(
                version_ids=f.version_ids,
                service=f.service.value,
                library_names=f.library_names,
                path=f.path,
                size=f.size,
                added_at=f.added_at,
                video_resolution=f.video_resolution,
                video_width=f.video_width,
                video_height=f.video_height,
                video_codec_family=f.video_codec_family,
                video_hdr=f.video_hdr,
                video_dolby_vision=f.video_dolby_vision,
                video_bitrate=f.video_bitrate,
                audio_codec_family=f.audio_codec_family,
                audio_channels=f.audio_channels,
                protected=f.protected,
            )
            for f in group.files
        ],
        manual_reason=group.manual_reason,
        cross_library=group.cross_library,
        ignored=group.ignored,
        reclaimable_size=group.reclaimable_size,
    )


@router.get("", response_model=PaginatedDuplicatesResponse)
async def list_duplicates(
    _user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=200),
    media_type: MediaType | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    sort_by: Literal["title", "size"] = Query(default="title"),
    include_cross_library: bool = Query(default=False),
    include_manual: bool = Query(default=True),
    include_ignored: bool = Query(default=False),
) -> PaginatedDuplicatesResponse:
    groups = await load_duplicate_groups(db, media_type=media_type, search=search)
    groups = [
        g
        for g in groups
        if (include_cross_library or not g.cross_library)
        and (include_manual or g.manual_reason is None)
        and (include_ignored or not g.ignored)
    ]
    if sort_by == "size":
        groups.sort(key=lambda g: g.reclaimable_size, reverse=True)

    actionable = [g for g in groups if g.manual_reason is None and not g.ignored]
    total = len(groups)
    start = (page - 1) * per_page
    return PaginatedDuplicatesResponse(
        items=[_serialize(g) for g in groups[start : start + per_page]],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=ceil(total / per_page) if total else 0,
        summary=DuplicateSummary(
            groups=total,
            actionable=len(actionable),
            reclaimable_size=sum(g.reclaimable_size for g in actionable),
        ),
    )


@router.post("/delete")
async def delete_duplicates(
    body: DuplicateDeleteRequest,
    user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Queue removal of the picked files; every group keeps at least one file."""
    _require_manage_reclaim(user)

    seen: set[tuple[MediaType, int]] = set()
    items: list[DuplicateDeleteJobItem] = []
    problems: list[str] = []
    for entry in body.items:
        key = (entry.media_type, entry.item_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            group, _selected = await plan_duplicate_delete(
                db,
                media_type=entry.media_type,
                item_id=entry.item_id,
                version_ids=entry.version_ids,
            )
        except DuplicateActionError as e:
            problems.append(f"{entry.media_type.value} {entry.item_id}: {e}")
            continue
        items.append(
            DuplicateDeleteJobItem(
                media_type=entry.media_type,
                item_id=entry.item_id,
                version_ids=entry.version_ids,
                display_label=_group_label(group),
            )
        )
    if problems:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="; ".join(problems[:5])
        )

    job = await queue_duplicate_delete_job(
        items=items,
        requested_by_user_id=user.id,
        requested_by_username=user.username,
    )
    count = len(items)
    return {
        "message": f"Queued duplicate cleanup for {count} item{'s' if count != 1 else ''}",
        "job_id": job.id,
    }


@router.post("/ignore")
async def ignore_duplicate(
    body: DuplicateIgnoreRequest,
    user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Mark a group as not a duplicate until its files change."""
    _require_manage_reclaim(user)
    group = await load_group(db, media_type=body.media_type, item_id=body.item_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Duplicate group not found")

    existing = (
        await db.execute(
            select(DuplicateIgnore).where(
                DuplicateIgnore.media_type == body.media_type,
                DuplicateIgnore.item_id == body.item_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            DuplicateIgnore(
                media_type=body.media_type,
                item_id=body.item_id,
                fingerprint=group.fingerprint,
            )
        )
    else:
        existing.fingerprint = group.fingerprint
    await db.commit()
    return {"message": "Marked as not a duplicate"}


@router.post("/unignore")
async def unignore_duplicate(
    body: DuplicateIgnoreRequest,
    user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    _require_manage_reclaim(user)
    await db.execute(
        delete(DuplicateIgnore).where(
            DuplicateIgnore.media_type == body.media_type,
            DuplicateIgnore.item_id == body.item_id,
        )
    )
    await db.commit()
    return {"message": "Duplicate restored"}


# ---------------------------------------------------------------------------
# upgrade leftovers
# ---------------------------------------------------------------------------


def _leftover_label(view: LeftoverView) -> str:
    row = view.row
    title = f"{row.title} ({row.year})" if row.year else row.title
    return f"{title} - upgrade leftover"


def _serialize_leftover(view: LeftoverView) -> UpgradeLeftoverResponse:
    row = view.row
    return UpgradeLeftoverResponse(
        id=row.id,
        movie_id=row.movie_id,
        title=row.title,
        year=row.year,
        poster_url=view.poster_url,
        source_title=row.source_title,
        dropped_path=row.dropped_path,
        size=row.size,
        link_count=row.link_count,
        frees_space=view.frees_space,
        imported_at=row.imported_at,
        manual_reason=row.manual_reason,
        ignored=row.ignored,
    )


@router.get("/leftovers", response_model=PaginatedLeftoversResponse)
async def list_leftovers(
    _user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=200),
    search: str | None = Query(default=None, max_length=200),
    sort_by: Literal["title", "size", "imported"] = Query(default="title"),
    include_manual: bool = Query(default=True),
    include_ignored: bool = Query(default=False),
) -> PaginatedLeftoversResponse:
    views = [
        v
        for v in await load_leftovers(db, search=search)
        if (include_manual or v.row.manual_reason is None)
        and (include_ignored or not v.row.ignored)
    ]
    if sort_by == "size":
        views.sort(key=lambda v: v.row.size, reverse=True)
    elif sort_by == "imported":
        views.sort(key=lambda v: v.row.imported_at or datetime.min)

    total = len(views)
    start = (page - 1) * per_page
    scanned_at, unmapped_roots = await load_scan_summary(db)
    return PaginatedLeftoversResponse(
        items=[_serialize_leftover(v) for v in views[start : start + per_page]],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=ceil(total / per_page) if total else 0,
        summary=LeftoverSummary(
            leftovers=total,
            actionable=sum(1 for v in views if v.actionable),
            reclaimable_size=sum(v.reclaimable_size for v in views),
        ),
        scan=LeftoverScanInfo(
            scanned_at=scanned_at,
            unmapped_roots=unmapped_roots,
            running=await is_task_active(Task.SCAN_UPGRADE_LEFTOVERS),
        ),
    )


@router.post("/leftovers/delete")
async def delete_leftovers(
    body: LeftoverDeleteRequest,
    user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Queue removal of the picked leftovers; each is re-checked before deletion."""
    _require_manage_reclaim(user)
    ids = list(dict.fromkeys(body.ids))
    views = {v.row.id: v for v in await load_leftovers(db, ids=ids)}
    problems = [
        f"leftover {i}: no longer listed - rescan" for i in ids if i not in views
    ]
    problems += [
        f"{v.row.title}: needs manual review: {v.row.manual_reason}"
        for v in views.values()
        if v.row.manual_reason
    ]
    if problems:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="; ".join(problems[:5])
        )

    job = await queue_duplicate_delete_job(
        items=[],
        leftovers=[
            LeftoverDeleteJobItem(id=i, display_label=_leftover_label(views[i]))
            for i in ids
        ],
        requested_by_user_id=user.id,
        requested_by_username=user.username,
    )
    count = len(ids)
    return {
        "message": f"Queued deletion of {count} leftover{'s' if count != 1 else ''}",
        "job_id": job.id,
    }


@router.post("/leftovers/scan")
async def scan_leftovers(
    user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
) -> dict[str, Any]:
    _require_manage_reclaim(user)
    try:
        job, queued = await request_task_run(Task.SCAN_UPGRADE_LEFTOVERS)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "message": "Scan queued" if queued else "A scan is already running",
        "job_id": job.id if job else None,
    }


async def _set_leftover_ignored(
    db: AsyncSession, leftover_id: int, ignored: bool
) -> None:
    row = await db.get(UpgradeLeftover, leftover_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Leftover not found")
    row.ignored = ignored
    await db.commit()


@router.post("/leftovers/ignore")
async def ignore_leftover(
    body: LeftoverIgnoreRequest,
    user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Hide a leftover; it stays hidden across rescans while the file exists."""
    _require_manage_reclaim(user)
    await _set_leftover_ignored(db, body.id, True)
    return {"message": "Leftover ignored"}


@router.post("/leftovers/unignore")
async def unignore_leftover(
    body: LeftoverIgnoreRequest,
    user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    _require_manage_reclaim(user)
    await _set_leftover_ignored(db, body.id, False)
    return {"message": "Leftover restored"}


@router.get("/settings", response_model=DuplicateSettings)
async def get_duplicate_settings(
    _user: Annotated[User, Depends(require_page_access(PageAccess.DUPLICATES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DuplicateSettings:
    priority = await load_keeper_priority(db)
    return DuplicateSettings(
        keeper_priority=[
            KeeperPriorityEntry.model_validate(entry)
            for entry in keeper_priority_payload(priority)
        ]
    )


@router.put("/settings", response_model=DuplicateSettings)
async def update_duplicate_settings(
    body: DuplicateSettings,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DuplicateSettings:
    priority = normalize_keeper_priority(
        [entry.model_dump() for entry in body.keeper_priority]
    )
    settings = (await db.execute(select(GeneralSettings).limit(1))).scalars().first()
    if settings is None:
        raise HTTPException(status_code=404, detail="General settings not found")
    settings.duplicate_keeper_priority = keeper_priority_payload(priority)
    await db.commit()
    return DuplicateSettings(
        keeper_priority=[
            KeeperPriorityEntry.model_validate(entry)
            for entry in keeper_priority_payload(priority)
        ]
    )
