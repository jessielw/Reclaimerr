from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import ReclaimCandidate, ReclaimRule
from backend.enums import MediaType

__all__ = ["ARR_ACTION_CHANGE_QUALITY_PROFILE", "non_reclaiming_candidate_totals"]

ARR_ACTION_CHANGE_QUALITY_PROFILE = "change_quality_profile"


async def non_reclaiming_candidate_totals(
    db: AsyncSession,
) -> dict[MediaType, tuple[int, int]]:
    """Totals for candidates whose action frees no space, per media type.

    A rule that only switches the quality profile leaves every file in place,
    so counting its candidates as reclaimable space would promise free space
    that never arrives. Callers subtract these figures from their own totals.

    Returns ``{media_type: (candidate_count, bytes)}``, empty when no rule asks
    for a profile change - which is the usual case, and costs one small query.
    """
    profile_rule_ids = {
        rule_id
        for rule_id, action in (
            await db.execute(select(ReclaimRule.id, ReclaimRule.action))
        ).all()
        if isinstance(action, dict)
        and action.get("arr_action") == ARR_ACTION_CHANGE_QUALITY_PROFILE
    }
    if not profile_rule_ids:
        return {}

    totals: dict[MediaType, tuple[int, int]] = {}
    rows = (
        await db.execute(
            select(
                ReclaimCandidate.media_type,
                ReclaimCandidate.matched_rule_ids,
                ReclaimCandidate.estimated_space_bytes,
            )
        )
    ).all()
    for media_type, matched_rule_ids, estimated_space_bytes in rows:
        if not profile_rule_ids.intersection(matched_rule_ids or []):
            continue
        count, total = totals.get(media_type, (0, 0))
        totals[media_type] = (count + 1, total + (estimated_space_bytes or 0))
    return totals
