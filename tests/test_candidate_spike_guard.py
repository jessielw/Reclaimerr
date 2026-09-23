"""A scan that suddenly adds far more candidates must not feed auto-delete directly.

Issue #401: bad watch data turned 264 series into candidates overnight, on a
rule with auto-delete enabled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.auto_delete import resolve_auto_delete_policy
from backend.database import Base
from backend.database.models import ReclaimCandidate
from backend.enums import MediaType
from backend.tasks.cleanup import (
    CANDIDATE_SPIKE_MIN_NEW,
    CANDIDATE_SPIKE_POSTPONE_DAYS,
    _is_candidate_spike,
    _postpone_candidate_spike,
)


def _candidate(series_id: int) -> ReclaimCandidate:
    return ReclaimCandidate(
        media_type=MediaType.SERIES,
        matched_rule_ids=[1],
        matched_criteria={},
        reason="view count 0",
        series_id=series_id,
    )


def test_spike_threshold() -> None:
    assert _is_candidate_spike(264, 6) is True
    # normal daily churn
    assert _is_candidate_spike(1, 6) is False
    # large absolute number, but the pool did not more than double
    assert _is_candidate_spike(100, 500) is False
    # small libraries never trip it
    assert _is_candidate_spike(CANDIDATE_SPIKE_MIN_NEW - 1, 0) is False


def _run_scan(existing: int, new: int) -> list[ReclaimCandidate]:
    async def run() -> list[ReclaimCandidate]:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db:
            db.add_all(_candidate(n) for n in range(existing))
            await db.commit()
            existing_ids = set((await db.scalars(select(ReclaimCandidate.id))).all())

            db.add_all(_candidate(1000 + n) for n in range(new))
            await _postpone_candidate_spike(db, existing_ids, new)
            await db.commit()
            rows = list(
                (
                    await db.scalars(
                        select(ReclaimCandidate).order_by(ReclaimCandidate.id)
                    )
                ).all()
            )
        await engine.dispose()
        return rows

    return asyncio.run(run())


def test_spike_postpones_only_new_candidates() -> None:
    rows = _run_scan(existing=6, new=264)

    old, new = rows[:6], rows[6:]
    assert all(row.auto_delete_postponed_until is None for row in old)
    assert all(row.auto_delete_postponed_until is not None for row in new)
    assert "Scan added 264 candidates" in (new[0].lifecycle_reason or "")

    now = datetime.now(UTC)
    policy = resolve_auto_delete_policy(
        media_type=MediaType.SERIES,
        matched_rule_ids=[1],
        created_at=now,
        postponed_until=new[0].auto_delete_postponed_until,
        rule_actions_by_id={1: {"auto_delete_enabled": True}},
        movie_delay_days=0,
        series_delay_days=0,
        now=now,
    )
    # even with no review period, nothing is eligible until the hold passes
    assert policy.state == "postponed"
    assert policy.is_eligible is False
    assert policy.eligible_at >= now + timedelta(
        days=CANDIDATE_SPIKE_POSTPONE_DAYS, minutes=-1
    )


def test_normal_scan_is_untouched() -> None:
    rows = _run_scan(existing=6, new=1)

    assert all(row.auto_delete_postponed_until is None for row in rows)
