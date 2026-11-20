"""Unified deletion intents, using the same policies and holds as cleanup."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auto_delete import resolve_auto_delete_policy
from backend.core.utils.datetime_utils import ensure_utc
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    Season,
    Series,
)
from backend.enums import MediaType
from backend.models.api_v1.leaving_soon import LeavingSoonItem
from backend.services.candidate_lifecycle import candidate_scope
from backend.services.media_server_items import MediaItemTarget, load_media_server_items
from backend.services.reclaim_stats import (
    SCHEDULED_AUTO_DELETE_STATES,
    load_auto_delete_inputs,
)
from backend.services.reclaimable import ARR_ACTION_CHANGE_QUALITY_PROFILE
from backend.services.scheduled_deletions import (
    active_schedules,
    blocking_schedule,
    inventory_cache,
    schedule_execution_state,
    scheduled_media_server_items,
)


async def load_leaving_soon(
    db: AsyncSession,
    *,
    media_type: MediaType | None,
    after: datetime | None,
    before: datetime | None,
    offset: int,
    limit: int,
) -> tuple[list[LeavingSoonItem], int]:
    rows = (
        await db.execute(
            select(ReclaimCandidate, Movie, MovieVersion, Series, Season, Episode)
            .outerjoin(Movie, ReclaimCandidate.movie_id == Movie.id)
            .outerjoin(
                MovieVersion, ReclaimCandidate.movie_version_id == MovieVersion.id
            )
            .outerjoin(Series, ReclaimCandidate.series_id == Series.id)
            .outerjoin(Season, ReclaimCandidate.season_id == Season.id)
            .outerjoin(Episode, ReclaimCandidate.episode_id == Episode.id)
        )
    ).all()
    inputs = await load_auto_delete_inputs(
        db,
        (
            rule_id
            for row in rows
            for rule_id in (row.ReclaimCandidate.matched_rule_ids or [])
        ),
    )
    schedules = await active_schedules(db)
    cache = await inventory_cache(db, [row.ReclaimCandidate for row in rows], schedules)
    now = datetime.now(UTC)
    items: list[LeavingSoonItem] = []
    for candidate, movie, version, series, season, episode in rows:
        actions = [
            inputs.rule_actions_by_id.get(rule_id) or {}
            for rule_id in (candidate.matched_rule_ids or [])
        ]
        if any(
            action.get("arr_action") == ARR_ACTION_CHANGE_QUALITY_PROFILE
            for action in actions
        ):
            continue
        policy = resolve_auto_delete_policy(
            media_type=candidate.media_type,
            matched_rule_ids=candidate.matched_rule_ids or [],
            created_at=candidate.created_at,
            timer_started_at=candidate.auto_delete_timer_started_at,
            postponed_until=candidate.auto_delete_postponed_until,
            cancelled_at=candidate.auto_delete_cancelled_at,
            rule_actions_by_id=inputs.rule_actions_by_id,
            movie_delay_days=inputs.movie_delay_days,
            series_delay_days=inputs.series_delay_days,
            now=now,
        )
        if not policy.is_enabled or policy.state not in SCHEDULED_AUTO_DELETE_STATES:
            continue
        media = movie if candidate.media_type is MediaType.MOVIE else series
        if media is None:
            continue
        if await blocking_schedule(db, candidate, schedules, cache):
            continue

        title = media.title
        if season is not None:
            title += f" S{season.season_number:02d}"
            if episode is not None:
                title += f"E{episode.episode_number:02d}"
        # A missing or unknown child size must never become the parent's size.
        if candidate.movie_version_id is not None:
            scoped_media = version
        elif candidate.episode_id is not None:
            scoped_media = episode
        elif candidate.season_id is not None:
            scoped_media = season
        else:
            scoped_media = media
        size = candidate.estimated_space_bytes
        if size is None and scoped_media is not None:
            size = scoped_media.size
        items.append(
            LeavingSoonItem(
                source="candidate",
                candidate_id=candidate.id,
                media_type=candidate.media_type,
                scope=candidate_scope(candidate),
                media_id=media.id,
                movie_version_id=candidate.movie_version_id,
                series_id=candidate.series_id,
                season_id=candidate.season_id,
                season_number=season.season_number if season else None,
                episode_id=candidate.episode_id,
                episode_number=episode.episode_number if episode else None,
                title=title,
                year=media.year,
                tmdb_id=media.tmdb_id,
                size_bytes=size,
                delete_operation="move"
                if any(
                    action.get("move_instead_of_delete") is True for action in actions
                )
                else "delete",
                delete_at=ensure_utc(policy.eligible_at),
                state=policy.state,
            )
        )
    for schedule in schedules:
        target = schedule.target
        if any(
            (action or {}).get("arr_action") == ARR_ACTION_CHANGE_QUALITY_PROFILE
            for action in (target.get("rule_actions") or {}).values()
        ):
            continue
        state, _ = await schedule_execution_state(db, schedule)
        # Select public fields explicitly: snapshots also contain paths and routing.
        items.append(
            LeavingSoonItem(
                source="delete_request"
                if schedule.delete_request_id is not None
                else "manual",
                candidate_id=target.get("candidate_id"),
                scheduled_deletion_id=schedule.id,
                delete_request_id=schedule.delete_request_id,
                media_type=target["media_type"],
                scope=target["scope"],
                media_id=target["media_id"],
                movie_version_id=target.get("movie_version_id"),
                series_id=target["media_id"]
                if target["media_type"] == "series"
                else None,
                season_id=target.get("season_id"),
                season_number=target.get("season_number"),
                episode_id=target.get("episode_id"),
                episode_number=target.get("episode_number"),
                title=target["title"],
                year=target.get("year"),
                tmdb_id=target.get("tmdb_id"),
                size_bytes=target.get("estimated_space_bytes"),
                delete_operation=target["operation"],
                delete_at=ensure_utc(schedule.scheduled_at),
                state=state,
            )
        )
    filtered = sorted(
        (
            item
            for item in items
            if (media_type is None or item.media_type == media_type)
            and (after is None or item.delete_at >= after)
            and (before is None or item.delete_at <= before)
        ),
        key=lambda item: (
            item.delete_at,
            item.source,
            item.scheduled_deletion_id or item.candidate_id or 0,
        ),
    )
    page = filtered[offset : offset + limit]
    candidate_targets = {
        index: MediaItemTarget(
            item.media_type,
            item.media_id,
            item.scope,
            movie_version_id=item.movie_version_id,
            season_id=item.season_id,
            episode_id=item.episode_id,
        )
        for index, item in enumerate(page)
        if item.scheduled_deletion_id is None
    }
    candidate_items = await load_media_server_items(db, candidate_targets.values())
    for index, target in candidate_targets.items():
        page[index].media_server_items = candidate_items[target]

    schedules_by_id = {schedule.id: schedule for schedule in schedules}
    scheduled_items = await scheduled_media_server_items(
        db,
        [
            schedules_by_id[item.scheduled_deletion_id]
            for item in page
            if item.scheduled_deletion_id is not None
        ],
    )
    for item in page:
        if item.scheduled_deletion_id is not None:
            item.media_server_items = scheduled_items[item.scheduled_deletion_id]
    return page, len(filtered)
