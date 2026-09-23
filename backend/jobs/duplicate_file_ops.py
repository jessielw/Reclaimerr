"""Background job that removes the files a user picked on the Duplicates page.

Deliberately separate from candidate file ops: a duplicate delete removes one
redundant file while the item keeps another, so it must never unmonitor, remove
the Arr entry or add an import exclusion. Per file the route is:

1. the Arr instance tracking that exact file (Radarr moviefile / Sonarr
   episodefile delete), which keeps the entry monitored with its other file;
2. otherwise the main media server, when media server fallback is enabled.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from backend.core.logger import LOG
from backend.core.protection_scope import detach_movie_version_references
from backend.core.service_manager import service_manager
from backend.core.utils.filesystem import (
    paths_equivalent,
    resolve_path,
    sibling_cleanup,
)
from backend.core.utils.request import summarize_error_message
from backend.core.workflow_locks import candidate_workflow_lock
from backend.database import async_db
from backend.database.models import (
    BackgroundJob,
    Episode,
    EpisodeVersion,
    GeneralSettings,
    Movie,
    MovieArrRef,
    MovieVersion,
    ReclaimHistory,
    Season,
    Series,
    SeriesArrRef,
)
from backend.enums import BackgroundJobPriority, BackgroundJobType, MediaType, Service
from backend.jobs.queue import enqueue_background_job, update_background_job_payload
from backend.models.jobs import (
    CandidateFileOpJobProgress,
    DuplicateDeleteJobItem,
    DuplicateDeleteJobPayload,
    DuplicateDeleteJobResult,
)
from backend.services.duplicates import (
    DuplicateActionError,
    DuplicateFile,
    DuplicateGroup,
    plan_duplicate_delete,
)
from backend.tasks.cleanup import (
    _best_effort_radarr_rescan,
    _best_effort_sonarr_refresh,
    _build_reclaim_history_attributes,
    _dispatch_reclaim_event,
    _reconcile_media_server_after_delete,
)


async def queue_duplicate_delete_job(
    *,
    items: list[DuplicateDeleteJobItem],
    requested_by_user_id: int,
    requested_by_username: str,
) -> BackgroundJob:
    payload = DuplicateDeleteJobPayload(
        items=items,
        requested_by_user_id=requested_by_user_id,
        requested_by_username=requested_by_username,
        item_labels=[item.display_label for item in items],
        progress=CandidateFileOpJobProgress(total_items=len(items)),
    ).model_dump(mode="json")
    job = await enqueue_background_job(
        job_type=BackgroundJobType.DUPLICATE_DELETE,
        payload=payload,
        priority=BackgroundJobPriority.HIGH,
    )
    if job is None:
        raise RuntimeError("Failed to queue duplicate delete")
    return job


async def run_duplicate_delete_job(
    job_id: int, payload: DuplicateDeleteJobPayload
) -> dict[str, Any]:
    """Serialized with candidate scans and file ops, which touch the same rows."""
    async with candidate_workflow_lock:
        return await _run_unlocked(job_id, payload)


async def _run_unlocked(
    job_id: int, payload: DuplicateDeleteJobPayload
) -> dict[str, Any]:
    total = len(payload.items)
    succeeded = 0
    failed = 0
    freed = 0
    errors: list[str] = []

    async def _progress(label: str | None, completed: int) -> None:
        percent = 100 if total == 0 else int(completed / total * 100)
        progress = CandidateFileOpJobProgress(
            total_items=total,
            completed_items=completed,
            failed_items=failed,
            current_item_label=label,
            percent=min(100, max(0, percent)),
        )
        await update_background_job_payload(
            job_id, {"progress": progress.model_dump(mode="json")}
        )

    for index, item in enumerate(payload.items):
        await _progress(item.display_label, index)
        try:
            freed += await delete_duplicate_files(
                item, approved_by=payload.requested_by_username
            )
            succeeded += 1
        except DuplicateActionError as e:
            failed += 1
            errors.append(f"{item.display_label}: {e}")
        except Exception as e:
            LOG.exception(f"Duplicate delete failed for {item.display_label}")
            failed += 1
            reason = summarize_error_message(str(e), max_chars=300) or str(e)
            errors.append(f"{item.display_label}: {reason}")
    await _progress(None, total)

    return DuplicateDeleteJobResult(
        processed=total,
        succeeded=succeeded,
        failed=failed,
        freed_bytes=freed,
        errors=errors,
    ).model_dump(mode="json")


async def delete_duplicate_files(
    item: DuplicateDeleteJobItem, *, approved_by: str
) -> int:
    """Delete the picked files of one group. Returns bytes freed.

    The group is re-planned from the database first, so a sync or another
    delete since the page loaded cannot make this remove the last file.
    """
    async with async_db() as db:
        group, selected = await plan_duplicate_delete(
            db,
            media_type=item.media_type,
            item_id=item.item_id,
            version_ids=item.version_ids,
        )
        settings = (
            (await db.execute(select(GeneralSettings).limit(1))).scalars().first()
        )
    mappings: list[dict[str, Any]] = (
        list(settings.path_mappings or []) if settings else []
    )
    fallback_enabled = settings.media_server_fallback_enabled if settings else True

    kept = [f for f in group.files if f not in selected]
    freed = 0
    for f in selected:
        if group.media_type is MediaType.MOVIE:
            await _delete_movie_file(
                group,
                f,
                kept,
                approved_by=approved_by,
                mappings=mappings,
                fallback_enabled=fallback_enabled,
            )
        else:
            await _delete_episode_file(
                group,
                f,
                kept,
                approved_by=approved_by,
                mappings=mappings,
                fallback_enabled=fallback_enabled,
            )
        freed += f.size
    return freed


def _arr_file_ids(
    entries: Sequence[dict[str, object]],
    f: DuplicateFile,
    mappings: list[dict[str, Any]],
    *,
    arr: Service,
    config_id: int,
) -> list[int]:
    ids: list[int] = []
    for entry in entries:
        entry_id = entry.get("id")
        entry_path = entry.get("path")
        if not isinstance(entry_id, int) or not isinstance(entry_path, str):
            continue
        if paths_equivalent(
            f.path,
            entry_path,
            mappings,
            left_service_type=f.service.value,
            right_service_type=arr.value,
            right_service_config_id=config_id,
        ):
            ids.append(entry_id)
    return ids


def _cleanup_sidecars(
    f: DuplicateFile, kept: Sequence[DuplicateFile], mappings: list[dict[str, Any]]
) -> Path | None:
    """Remove subtitle/nfo leftovers after a media-server delete.

    sibling_cleanup removes every file sharing the deleted file's stem, so it is
    skipped when a kept copy shares that stem (Movie.mkv next to Movie.mp4).
    """
    local = resolve_path(f.path, mappings, service_type=f.service.value)
    if local is None:
        return None
    for other in kept:
        other_local = resolve_path(
            other.path, mappings, service_type=other.service.value, warn_missing=False
        )
        if (
            other_local is not None
            and other_local.parent == local.parent
            and other_local.stem == local.stem
        ):
            LOG.info(
                f"Skipping sidecar cleanup for '{f.path}': a kept file shares its name"
            )
            return local
    try:
        sibling_cleanup(local)
    except Exception as e:
        LOG.warning(f"sibling_cleanup failed for '{f.path}': {e}")
    return local


async def _media_server_delete(
    f: DuplicateFile,
    pairs: set[tuple[str, str]],
    kept: Sequence[DuplicateFile],
    mappings: list[dict[str, Any]],
    *,
    fallback_enabled: bool,
    arr_name: str,
) -> tuple[Service, Path | None]:
    if not fallback_enabled:
        raise DuplicateActionError(
            f"{arr_name} doesn't track this file and media server fallback "
            "deletion is turned off in General Settings"
        )
    main = service_manager.main_media_server
    main_type = service_manager.main_media_server_type
    if main is None or main_type is None:
        raise DuplicateActionError("No main media server configured")
    if f.service != main_type:
        raise DuplicateActionError(
            f"File belongs to {f.service.value}, main media server is {main_type.value}"
        )
    # Plex removes the one Media entry; Jellyfin/Emby remove the item, which the
    # manual-review check guarantees holds only this file.
    for item_id, media_id in sorted(pairs):
        await main.delete_movie_version(item_id, media_id)
    return main_type, _cleanup_sidecars(f, kept, mappings)


async def _delete_movie_file(
    group: DuplicateGroup,
    f: DuplicateFile,
    kept: Sequence[DuplicateFile],
    *,
    approved_by: str,
    mappings: list[dict[str, Any]],
    fallback_enabled: bool,
) -> None:
    async with async_db() as db:
        movie = await db.get(Movie, group.item_id)
        rows = (
            (
                await db.execute(
                    select(MovieVersion).where(MovieVersion.id.in_(f.version_ids))
                )
            )
            .scalars()
            .all()
        )
        refs = (
            await db.execute(
                select(MovieArrRef.service_config_id, MovieArrRef.arr_movie_id).where(
                    MovieArrRef.movie_id == group.item_id
                )
            )
        ).all()
        history_attrs = (
            _build_reclaim_history_attributes(movie_version=rows[0]) if rows else None
        )
    if movie is None or not rows:
        raise DuplicateActionError("Movie no longer in the database - refresh the page")
    title, tmdb_id = movie.title, movie.tmdb_id

    clients = service_manager.radarr_clients()
    if not clients and service_manager.radarr:
        clients = {0: service_manager.radarr}
    routed: dict[int, set[int]] = {}
    for config_id, arr_movie_id in refs:
        client = clients.get(config_id)
        if client is None:
            continue
        file_ids = _arr_file_ids(
            await client.get_movie_files(arr_movie_id),
            f,
            mappings,
            arr=Service.RADARR,
            config_id=config_id,
        )
        if file_ids:
            await client.delete_movie_files(file_ids)
            routed.setdefault(config_id, set()).add(arr_movie_id)
            LOG.info(
                f"Duplicate delete: removed '{f.path}' for '{title}' via Radarr "
                f"(config_id={config_id}, file_ids={file_ids})"
            )

    local_path: Path | None = None
    if routed:
        via = Service.RADARR
    else:
        via, local_path = await _media_server_delete(
            f,
            {(r.service_item_id, r.service_media_id) for r in rows},
            kept,
            mappings,
            fallback_enabled=fallback_enabled,
            arr_name="Radarr",
        )
        LOG.info(f"Duplicate delete: removed '{f.path}' for '{title}' via {via.value}")

    async with async_db() as db:
        movie_db = await db.get(Movie, group.item_id)
        if movie_db is not None and movie_db.size:
            movie_db.size = max(0, movie_db.size - f.size)
        await detach_movie_version_references(db, f.version_ids)
        await db.execute(delete(MovieVersion).where(MovieVersion.id.in_(f.version_ids)))
        db.add(
            ReclaimHistory(
                approved_by=approved_by,
                media_type=MediaType.MOVIE,
                tmdb_id=tmdb_id,
                name=title,
                path=f.path,
                size=f.size,
                attributes=history_attrs,
            )
        )
        await db.commit()

    if routed:
        await _reconcile_media_server_after_delete(
            item_id=None,
            paths=[f.path],
            allow_delete_item=False,
            context=f"duplicate delete '{title}'",
        )
        await _best_effort_radarr_rescan(routed, context="duplicate delete")
    await _dispatch_reclaim_event(
        action="deleted",
        media_type=MediaType.MOVIE,
        title=title,
        tmdb_id=tmdb_id,
        path=f.path,
        local_path=str(local_path) if local_path else None,
        service_type=via,
        movie_version_id=rows[0].id,
    )


async def _delete_episode_file(
    group: DuplicateGroup,
    f: DuplicateFile,
    kept: Sequence[DuplicateFile],
    *,
    approved_by: str,
    mappings: list[dict[str, Any]],
    fallback_enabled: bool,
) -> None:
    async with async_db() as db:
        episode = await db.get(Episode, group.item_id)
        series = await db.get(Series, group.series_id) if group.series_id else None
        rows = (
            (
                await db.execute(
                    select(EpisodeVersion).where(EpisodeVersion.id.in_(f.version_ids))
                )
            )
            .scalars()
            .all()
        )
        refs = (
            await db.execute(
                select(
                    SeriesArrRef.service_config_id, SeriesArrRef.arr_series_id
                ).where(SeriesArrRef.series_id == group.series_id)
            )
        ).all()
    if episode is None or series is None or not rows:
        raise DuplicateActionError(
            "Episode no longer in the database - refresh the page"
        )
    label = f"{series.title} S{group.season_number or 0:02d}E{group.episode_number or 0:02d}"

    clients = service_manager.sonarr_clients()
    if not clients and service_manager.sonarr:
        clients = {0: service_manager.sonarr}
    routed: dict[int, set[int]] = {}
    for config_id, arr_series_id in refs:
        client = clients.get(config_id)
        if client is None:
            continue
        file_ids = _arr_file_ids(
            await client.get_episode_files(arr_series_id),
            f,
            mappings,
            arr=Service.SONARR,
            config_id=config_id,
        )
        for file_id in file_ids:
            await client.delete_episode_file(file_id)
        if file_ids:
            routed.setdefault(config_id, set()).add(arr_series_id)
            LOG.info(
                f"Duplicate delete: removed '{f.path}' for {label} via Sonarr "
                f"(config_id={config_id}, file_ids={file_ids})"
            )

    local_path: Path | None = None
    if routed:
        via = Service.SONARR
    else:
        via, local_path = await _media_server_delete(
            f,
            {(r.service_item_id, r.service_media_id) for r in rows},
            kept,
            mappings,
            fallback_enabled=fallback_enabled,
            arr_name="Sonarr",
        )
        LOG.info(f"Duplicate delete: removed '{f.path}' for {label} via {via.value}")

    async with async_db() as db:
        await db.execute(
            delete(EpisodeVersion).where(EpisodeVersion.id.in_(f.version_ids))
        )
        episode_db = await db.get(Episode, group.item_id)
        if episode_db is not None and episode_db.path == f.path and kept:
            # the primary file is gone; point the episode at the best kept copy
            episode_db.path = kept[0].path
            episode_db.size = kept[0].size
        season_db = await db.get(Season, group.season_id) if group.season_id else None
        if season_db is not None and season_db.size:
            season_db.size = max(0, season_db.size - f.size)
        series_db = await db.get(Series, group.series_id) if group.series_id else None
        if series_db is not None and series_db.size:
            series_db.size = max(0, series_db.size - f.size)
        db.add(
            ReclaimHistory(
                approved_by=approved_by,
                media_type=MediaType.SERIES,
                tmdb_id=series.tmdb_id,
                name=label,
                path=f.path,
                size=f.size,
                attributes={
                    "resolution": f.video_resolution,
                    "hdr": f.video_hdr,
                    "dolby_vision": f.video_dolby_vision,
                },
            )
        )
        await db.commit()

    if routed:
        await _reconcile_media_server_after_delete(
            item_id=None,
            paths=[f.path],
            allow_delete_item=False,
            context=f"duplicate delete {label}",
        )
        await _best_effort_sonarr_refresh(routed, context="duplicate delete")
    await _dispatch_reclaim_event(
        action="deleted",
        media_type=MediaType.SERIES,
        title=series.title,
        tmdb_id=series.tmdb_id,
        path=f.path,
        local_path=str(local_path) if local_path else None,
        service_type=via,
        season_id=group.season_id,
        season_number=group.season_number,
        episode_id=group.item_id,
        episode_number=group.episode_number,
    )
