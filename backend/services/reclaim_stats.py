"""Reclaim totals shared by the dashboard, the calendar and the external stats API.

Kept in one place so a dashboard widget fed by ``/api/v1/stats`` can never show
a different number than Reclaimerr's own dashboard for the same data.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auto_delete import resolve_auto_delete_policy
from backend.core.utils.datetime_utils import ensure_utc
from backend.database.models import (
    GeneralSettings,
    ReclaimCandidate,
    ReclaimHistory,
    ReclaimRule,
)
from backend.enums import MediaType
from backend.services.arr_readd import READD_ACTION
from backend.services.reclaimable import non_reclaiming_candidate_totals

# Auto-delete states that end in a deletion or move without anyone acting.
SCHEDULED_AUTO_DELETE_STATES = frozenset({"scheduled", "eligible", "postponed"})


@dataclass(frozen=True, slots=True)
class ReclaimTotals:
    reclaimable_movies_bytes: int
    reclaimable_series_bytes: int
    reclaimed_movies: int
    reclaimed_series: int
    reclaimed_bytes: int


async def load_reclaim_totals(db: AsyncSession) -> ReclaimTotals:
    """Space candidates would free, and what has already been reclaimed."""
    reclaimable = dict(
        (
            await db.execute(
                select(
                    ReclaimCandidate.media_type,
                    func.coalesce(func.sum(ReclaimCandidate.estimated_space_bytes), 0),
                ).group_by(ReclaimCandidate.media_type)
            )
        )
        .tuples()
        .all()
    )
    # a profile-change candidate leaves its files in place, so its bytes are
    # not space anyone is going to get back
    non_reclaiming = await non_reclaiming_candidate_totals(db)

    def reclaimable_bytes(media_type: MediaType) -> int:
        total = int(reclaimable.get(media_type) or 0)
        return max(0, total - non_reclaiming.get(media_type, (0, 0))[1])

    # a re-added title is History, not reclaimed space
    reclaimed = ReclaimHistory.action != READD_ACTION
    reclaimed_counts = dict(
        (
            await db.execute(
                select(ReclaimHistory.media_type, func.count())
                .where(reclaimed)
                .group_by(ReclaimHistory.media_type)
            )
        )
        .tuples()
        .all()
    )
    reclaimed_bytes = await db.scalar(
        select(func.coalesce(func.sum(ReclaimHistory.size), 0)).where(reclaimed)
    )
    return ReclaimTotals(
        reclaimable_movies_bytes=reclaimable_bytes(MediaType.MOVIE),
        reclaimable_series_bytes=reclaimable_bytes(MediaType.SERIES),
        reclaimed_movies=int(reclaimed_counts.get(MediaType.MOVIE) or 0),
        reclaimed_series=int(reclaimed_counts.get(MediaType.SERIES) or 0),
        reclaimed_bytes=int(reclaimed_bytes or 0),
    )


@dataclass(frozen=True, slots=True)
class AutoDeleteInputs:
    rule_actions_by_id: dict[int, dict[str, Any] | None]
    movie_delay_days: int
    series_delay_days: int


async def load_auto_delete_inputs(
    db: AsyncSession, rule_ids: Iterable[int]
) -> AutoDeleteInputs:
    """Load what ``resolve_auto_delete_policy`` needs besides the candidate."""
    settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
    ids = set(rule_ids)
    rule_actions_by_id: dict[int, dict[str, Any] | None] = {}
    if ids:
        rule_actions_by_id = {
            rule.id: rule.action
            for rule in (
                await db.execute(select(ReclaimRule).where(ReclaimRule.id.in_(ids)))
            )
            .scalars()
            .all()
        }
    return AutoDeleteInputs(
        rule_actions_by_id=rule_actions_by_id,
        movie_delay_days=(
            settings_row.auto_delete_movie_delay_days if settings_row else 14
        ),
        series_delay_days=(
            settings_row.auto_delete_series_delay_days if settings_row else 7
        ),
    )


async def next_auto_delete_at(
    db: AsyncSession, now: datetime | None = None
) -> datetime | None:
    """The earliest deadline among candidates set to be deleted automatically.

    It can be in the past: an eligible candidate waits for the next run of the
    automatic deletion task.
    """
    candidates = (await db.execute(select(ReclaimCandidate))).scalars().all()
    if not candidates:
        return None
    inputs = await load_auto_delete_inputs(
        db,
        (
            rule_id
            for candidate in candidates
            for rule_id in (candidate.matched_rule_ids or [])
        ),
    )
    resolved_now = now or datetime.now(UTC)
    deadlines = []
    for candidate in candidates:
        policy = resolve_auto_delete_policy(
            media_type=cast(MediaType, candidate.media_type),
            matched_rule_ids=cast(list[int], candidate.matched_rule_ids or []),
            created_at=cast(datetime, candidate.created_at),
            timer_started_at=candidate.auto_delete_timer_started_at,
            postponed_until=candidate.auto_delete_postponed_until,
            cancelled_at=candidate.auto_delete_cancelled_at,
            rule_actions_by_id=inputs.rule_actions_by_id,
            movie_delay_days=inputs.movie_delay_days,
            series_delay_days=inputs.series_delay_days,
            now=resolved_now,
        )
        if policy.is_enabled and policy.state in SCHEDULED_AUTO_DELETE_STATES:
            deadlines.append(ensure_utc(policy.eligible_at))
    return min(deadlines, default=None)
