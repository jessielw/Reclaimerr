from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import has_permission
from backend.core.utils.datetime_utils import ensure_utc
from backend.core.utils.filesystem import mapped_path_variants, normalize_fpath
from backend.database.models import (
    BackgroundJob,
    Episode,
    EpisodeVersion,
    GeneralSettings,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    ReclaimRule,
    ScheduledDeletion,
    Season,
    Series,
    SeriesServiceRef,
    User,
)
from backend.enums import (
    BackgroundJobPriority,
    BackgroundJobStatus,
    BackgroundJobType,
    CandidateFileOpOperation,
    MediaType,
    Permission,
    UserRole,
)
from backend.models.jobs import CandidateFileOpJobPayload
from backend.models.scheduled_deletions import ScheduledDeletionResponse

ACTIVE_SCHEDULE_STATES = ("scheduled", "running", "blocked", "failed")
executing_schedule_id: ContextVar[int | None] = ContextVar(
    "executing_schedule_id", default=None
)


def require_delete_permission(user: User) -> None:
    if not user.is_active:
        raise HTTPException(
            403, "An active account with deletion permission is required"
        )
    if user.role is not UserRole.ADMIN and not has_permission(
        user, Permission.MANAGE_RECLAIM
    ):
        raise HTTPException(403, "Manage reclaim permission required")


def future_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(422, "A timezone is required")
    value = ensure_utc(value)
    if value <= datetime.now(UTC):
        raise HTTPException(422, "Deletion time must be in the future")
    return value


def _path(value: str) -> str:
    # Do not case-fold: Linux files that differ only in case are distinct.
    return normalize_fpath(value, strip_ending_slash=True)


async def routing_settings(db: AsyncSession, media_type: MediaType) -> dict[str, Any]:
    settings = await db.scalar(select(GeneralSettings).limit(1)) or GeneralSettings()
    destination = (
        "move_destination_movies"
        if media_type is MediaType.MOVIE
        else "move_destination_series"
    )
    return {
        name: getattr(settings, name)
        for name in (
            "default_arr_delete_behavior",
            "media_server_fallback_enabled",
            "add_arr_import_exclusions_on_delete",
            "path_mappings",
            destination,
        )
    }


async def target_inventory(
    db: AsyncSession, candidate: ReclaimCandidate
) -> dict[str, Any]:
    """Physical inventory, independent of IDs replaced during a media resync."""
    files: dict[str, int | None] = {}
    directories: set[str] = set()
    unidentified_files = 0

    def add(path: str | None, size: int | None = None) -> None:
        if path:
            key = _path(path)
            if not key:
                return
            if size or key not in files:
                files[key] = size or None

    if candidate.media_type is MediaType.MOVIE:
        query = select(MovieVersion).where(MovieVersion.movie_id == candidate.movie_id)
        if candidate.movie_version_id is not None:
            query = query.where(MovieVersion.id == candidate.movie_version_id)
        for version in (await db.scalars(query)).all():
            if not _path(version.path or ""):
                unidentified_files += 1
            add(version.path, version.size)
    else:
        query = select(Season).where(Season.series_id == candidate.series_id)
        if candidate.season_id is not None:
            query = query.where(Season.id == candidate.season_id)
        seasons = (await db.scalars(query)).all()
        episode_query = select(Episode).where(
            Episode.season_id.in_([s.id for s in seasons])
        )
        if candidate.episode_id is not None:
            episode_query = episode_query.where(Episode.id == candidate.episode_id)
        episodes = (await db.scalars(episode_query)).all()
        for episode in episodes:
            add(episode.path, episode.size)
        versions = (
            await db.scalars(
                select(EpisodeVersion).where(
                    EpisodeVersion.episode_id.in_([e.id for e in episodes])
                )
            )
        ).all()
        represented_episode_ids = {version.episode_id for version in versions}
        for episode in episodes:
            if (
                not _path(episode.path or "")
                and episode.size
                and episode.id not in represented_episode_ids
            ):
                unidentified_files += 1
        for version in versions:
            if not _path(version.path or ""):
                unidentified_files += 1
            add(version.path, version.size)
        if candidate.episode_id is None:
            roots = {
                _path(path)
                for path in (
                    await db.scalars(
                        select(SeriesServiceRef.path).where(
                            SeriesServiceRef.series_id == candidate.series_id
                        )
                    )
                ).all()
                if path and _path(path)
            }
            for season in seasons:
                for path in season.episode_paths or []:
                    add(path)
                if (
                    season.path
                    and _path(season.path)
                    and (candidate.season_id is None or _path(season.path) not in roots)
                ):
                    directories.add(_path(season.path))
            if candidate.season_id is None:
                directories.update(roots)
    return {
        "files": dict(sorted(files.items())),
        "directories": sorted(directories),
        "unidentified_files": unidentified_files,
    }


async def snapshot_target(
    db: AsyncSession, candidate: ReclaimCandidate
) -> dict[str, Any]:
    from backend.services.candidate_lifecycle import candidate_scope

    media = await db.get(
        Movie if candidate.media_type is MediaType.MOVIE else Series,
        candidate.movie_id or candidate.series_id,
    )
    if media is None or media.removed_at is not None:
        raise HTTPException(409, "The selected media is no longer available")
    season = await db.get(Season, candidate.season_id) if candidate.season_id else None
    episode = (
        await db.get(Episode, candidate.episode_id) if candidate.episode_id else None
    )
    if (
        candidate.season_id
        and (season is None or season.series_id != candidate.series_id)
    ) or (
        candidate.episode_id
        and (episode is None or episode.season_id != candidate.season_id)
    ):
        raise HTTPException(409, "The selected scope is no longer available")
    inventory = await target_inventory(db, candidate)
    if not inventory["files"] or inventory["unidentified_files"]:
        raise HTTPException(
            409, "Cannot schedule media without an identifiable file inventory"
        )
    rules = (
        await db.scalars(
            select(ReclaimRule).where(
                ReclaimRule.id.in_(candidate.matched_rule_ids or [])
            )
        )
    ).all()
    actions = {str(r.id): r.action for r in rules}
    title = media.title
    if season:
        title += f" S{season.season_number:02d}"
    if episode:
        title += f"E{episode.episode_number:02d}"
    version = (
        await db.get(MovieVersion, candidate.movie_version_id)
        if candidate.movie_version_id
        else None
    )
    return {
        "candidate_id": candidate.id,
        "media_type": candidate.media_type.value,
        "media_id": media.id,
        "scope": candidate_scope(candidate),
        "title": title,
        "year": media.year,
        "tmdb_id": media.tmdb_id,
        "movie_version_id": candidate.movie_version_id,
        "version_path": _path(version.path) if version and version.path else None,
        "season_id": candidate.season_id,
        "episode_id": candidate.episode_id,
        "season_number": season.season_number if season else None,
        "episode_number": episode.episode_number if episode else None,
        "estimated_space_bytes": candidate.estimated_space_bytes,
        "matched_rule_ids": list(candidate.matched_rule_ids or []),
        "rule_actions": actions,
        "routing_settings": await routing_settings(db, candidate.media_type),
        "operation": "move"
        if any((r.action or {}).get("move_instead_of_delete") is True for r in rules)
        else "delete",
        "inventory": inventory,
    }


def inventories_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    def hold_path(path: str) -> str:
        path = _path(path)
        # Holds must recognize ordinary Windows aliases even when the server
        # and the local filesystem disagree on drive/path capitalization.
        if (len(path) >= 3 and path[1:3] == ":/") or path.startswith("//"):
            return path.casefold()
        return path

    left_files = {hold_path(path) for path in left.get("files", {})}
    right_files = {hold_path(path) for path in right.get("files", {})}
    if left_files & right_files:
        return True
    return any(
        path == folder or path.startswith(folder + "/")
        for paths, folders in (
            (left_files, [hold_path(path) for path in right.get("directories", [])]),
            (right_files, [hold_path(path) for path in left.get("directories", [])]),
        )
        for path in paths
        for folder in folders
    )


def schedule_inventory_overlap(
    inventory: dict[str, Any], target: dict[str, Any]
) -> bool:
    saved = target["inventory"]
    if inventories_overlap(inventory, saved):
        return True
    mappings = target.get("routing_settings", {}).get("path_mappings") or []
    if not mappings:
        return False
    # For a hold, ambiguity is conservative: any configured representation of
    # a scheduled path must be kept. Execution still validates the exact scope.
    aliases = [
        {
            "source_prefix": mapping.get("source_prefix"),
            "local_prefix": mapping.get("local_prefix"),
        }
        for mapping in mappings
    ]

    def expand(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "files": {
                variant: None
                for path in value.get("files", {})
                for variant in mapped_path_variants(path, aliases)
            },
            "directories": [
                variant
                for path in value.get("directories", [])
                for variant in mapped_path_variants(path, aliases)
            ],
        }

    return inventories_overlap(expand(inventory), expand(saved))


async def blocking_file_paths(
    db: AsyncSession, paths: Iterable[str | None]
) -> ScheduledDeletion | None:
    inventory = {
        "files": {_path(path): None for path in paths if path},
        "directories": [],
    }
    if not inventory["files"]:
        return None
    for schedule in await active_schedules(db):
        if schedule.id != executing_schedule_id.get() and schedule_inventory_overlap(
            inventory, schedule.target
        ):
            return schedule
    return None


async def active_schedules(db: AsyncSession) -> list[ScheduledDeletion]:
    return list(
        (
            await db.scalars(
                select(ScheduledDeletion).where(
                    ScheduledDeletion.state.in_(ACTIVE_SCHEDULE_STATES)
                )
            )
        ).all()
    )


async def blocking_schedule(
    db: AsyncSession,
    candidate: ReclaimCandidate,
    schedules: list[ScheduledDeletion] | None = None,
) -> ScheduledDeletion | None:
    schedules = await active_schedules(db) if schedules is None else schedules
    relevant = [s for s in schedules if s.id != executing_schedule_id.get()]
    if not relevant:
        return None
    parent_id = candidate.movie_id or candidate.series_id
    # Season/episode rows and the file inventory are loaded lazily; only same
    # media needs the former and only non-scope matches need the latter.
    inventory: dict[str, Any] | None = None
    season = episode = None
    scope_loaded = False
    for schedule in relevant:
        target = schedule.target
        if not scope_loaded and (
            candidate.media_type is MediaType.SERIES
            and target["media_type"] == candidate.media_type.value
            and target["media_id"] == parent_id
        ):
            scope_loaded = True
            if candidate.season_id is not None:
                season = await db.get(Season, candidate.season_id)
            if candidate.episode_id is not None:
                episode = await db.get(Episode, candidate.episode_id)
        # Parent scope also blocks new/replaced files until the user cancels the
        # intent. A changed inventory must not silently release automatic deletion.
        same_parent = target["media_type"] == candidate.media_type.value and target[
            "media_id"
        ] == (candidate.movie_id or candidate.series_id)
        scope_overlap = same_parent and (
            (
                candidate.media_type is MediaType.MOVIE
                and (
                    target["scope"] == "movie"
                    or candidate.movie_version_id is None
                    or target.get("movie_version_id") == candidate.movie_version_id
                )
            )
            or (
                candidate.media_type is MediaType.SERIES
                and (
                    target["scope"] == "series"
                    or candidate.season_id is None
                    or (
                        season is not None
                        and target.get("season_number") == season.season_number
                        and (
                            target["scope"] == "season"
                            or candidate.episode_id is None
                            or (
                                episode is not None
                                and target.get("episode_number")
                                == episode.episode_number
                            )
                        )
                    )
                )
            )
        )
        if scope_overlap:
            return schedule
        if inventory is None:
            inventory = await target_inventory(db, candidate)
        if schedule_inventory_overlap(inventory, target):
            return schedule
    return None


async def resolve_target(db: AsyncSession, target: dict[str, Any]) -> ReclaimCandidate:
    """Rebuild only the saved scope. Never reinterpret missing child IDs as parents."""
    media_type = MediaType(target["media_type"])
    media = await db.get(
        Movie if media_type is MediaType.MOVIE else Series, target["media_id"]
    )
    if (
        media is None
        or media.removed_at is not None
        or media.tmdb_id != target["tmdb_id"]
    ):
        raise HTTPException(409, "The scheduled media is no longer available")
    version_id = season_id = episode_id = None
    if target["scope"] == "version":
        versions = (
            await db.scalars(
                select(MovieVersion).where(MovieVersion.movie_id == media.id)
            )
        ).all()
        matches = [
            v for v in versions if v.path and _path(v.path) == target["version_path"]
        ]
        if not matches:
            raise HTTPException(
                409, "The scheduled movie version is no longer available"
            )
        # Copies of the same physical file in several libraries remain one file.
        version_id = next(
            (v.id for v in matches if v.id == target["movie_version_id"]), matches[0].id
        )
    if target["scope"] in ("season", "episode"):
        season = await db.scalar(
            select(Season).where(
                Season.series_id == media.id,
                Season.season_number == target["season_number"],
            )
        )
        if season is None:
            raise HTTPException(409, "The scheduled season is no longer available")
        season_id = season.id
        if target["scope"] == "episode":
            episode = await db.scalar(
                select(Episode).where(
                    Episode.season_id == season.id,
                    Episode.episode_number == target["episode_number"],
                )
            )
            if episode is None:
                raise HTTPException(409, "The scheduled episode is no longer available")
            episode_id = episode.id
    candidate = ReclaimCandidate(
        media_type=media_type,
        movie_id=media.id if media_type is MediaType.MOVIE else None,
        series_id=media.id if media_type is MediaType.SERIES else None,
        movie_version_id=version_id,
        season_id=season_id,
        episode_id=episode_id,
        matched_rule_ids=target["matched_rule_ids"],
        matched_criteria={},
        reason="Manually scheduled deletion",
        reviewed=True,
        approved_for_deletion=True,
        estimated_space_bytes=target.get("estimated_space_bytes"),
    )
    if await target_inventory(db, candidate) != target["inventory"]:
        raise HTTPException(
            409,
            "The scheduled file inventory changed; cancel and schedule the current files again",
        )
    rules = (
        await db.scalars(
            select(ReclaimRule).where(ReclaimRule.id.in_(target["matched_rule_ids"]))
        )
    ).all()
    if {str(r.id): r.action for r in rules} != target["rule_actions"]:
        raise HTTPException(
            409,
            "The deletion rule actions changed; cancel and schedule again to confirm the new behavior",
        )
    if await routing_settings(db, media_type) != target["routing_settings"]:
        raise HTTPException(
            409,
            "The deletion routing settings changed; cancel and schedule again to confirm the new behavior",
        )
    return candidate


async def add_schedule_job(
    db: AsyncSession, schedule: ScheduledDeletion
) -> BackgroundJob:
    payload = CandidateFileOpJobPayload(
        operation=CandidateFileOpOperation.DELETE,
        candidate_ids=[],
        requested_by_user_id=schedule.created_by_user_id or 0,
        requested_by_username=schedule.created_by_username,
        scheduled_deletion_id=schedule.id,
        item_labels=[schedule.target["title"]],
        item_label_total=1,
    ).model_dump(mode="json")
    job = BackgroundJob(
        job_type=BackgroundJobType.CANDIDATE_FILE_OP,
        scheduled_at=ensure_utc(schedule.scheduled_at),
        payload=payload,
        priority=BackgroundJobPriority.HIGH,
        max_attempts=1,
    )
    db.add(job)
    await db.flush()
    schedule.job_id = job.id
    return job


async def schedule_response(
    db: AsyncSession, schedule: ScheduledDeletion
) -> ScheduledDeletionResponse:
    job = await db.get(BackgroundJob, schedule.job_id) if schedule.job_id else None
    state = schedule.state
    error = schedule.last_error
    if state in ("scheduled", "running"):
        if job is None:
            state, error = (
                "failed",
                "The execution job is no longer available; reschedule or cancel this deletion",
            )
        elif job.status is BackgroundJobStatus.FAILED:
            state, error = "failed", job.error_message
        elif job.status is BackgroundJobStatus.CANCELED:
            state, error = (
                "blocked",
                "The execution job was canceled; reschedule or cancel this deletion",
            )
    return ScheduledDeletionResponse(
        id=schedule.id,
        scheduled_at=ensure_utc(schedule.scheduled_at),
        state=state,
        target=schedule.target,
        created_by_username=schedule.created_by_username,
        last_error=error,
    )
