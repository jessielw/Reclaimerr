from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Movie,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    Series,
    User,
)
from backend.enums import MediaType, UserRole
from backend.tasks.sync import (
    MAX_SOFT_DELETE_RATIO,
    MIN_LIBRARY_FOR_RATIO_CHECK,
    _apply_soft_deletes,
    _select_rows_to_soft_delete,
    _soft_delete_guard_tripped,
)


def _series(row_id: int, tmdb_id: int, removed: bool = False) -> Series:
    series = Series(title=f"Series {row_id}", tmdb_id=tmdb_id, size=1)
    series.id = row_id
    series.removed_at = datetime.now(UTC) if removed else None
    return series


def _movie(row_id: int, tmdb_id: int, removed: bool = False) -> Movie:
    movie = Movie(title=f"Movie {row_id}", tmdb_id=tmdb_id, size=1)
    movie.id = row_id
    movie.removed_at = datetime.now(UTC) if removed else None
    return movie


def test_untouched_row_is_selected() -> None:
    rows = [_series(1, 1001), _series(2, 1002)]

    result = _select_rows_to_soft_delete(rows, {1})

    assert [r.id for r in result] == [2]


def test_row_matched_under_a_different_tmdb_id_is_not_selected() -> None:
    """The regression this whole change exists for.

    The row holds tmdb 1001. The main server reported the same show under
    tmdb 1002 and the tvdb fallback matched it, so the row was updated. Keying
    the delete pass on tmdb ids would tombstone a row that is still live.
    """
    row = _series(1, 1001)

    result = _select_rows_to_soft_delete([row], matched_row_ids={1})

    assert result == []


def test_already_tombstoned_row_is_not_selected_again() -> None:
    rows = [_series(1, 1001, removed=True)]

    result = _select_rows_to_soft_delete(rows, set())

    assert result == []


def test_movies_use_the_same_selection() -> None:
    rows = [_movie(1, 2001), _movie(2, 2002)]

    result = _select_rows_to_soft_delete(rows, {2})

    assert [r.id for r in result] == [1]


def test_guard_trips_when_delete_set_exceeds_the_ratio() -> None:
    assert _soft_delete_guard_tripped(delete_count=60, live_count=100) is True


def test_guard_allows_a_delete_set_at_the_ratio() -> None:
    assert _soft_delete_guard_tripped(delete_count=50, live_count=100) is False


def test_guard_is_disabled_for_small_libraries() -> None:
    """Removing three of four items is legitimate; do not block it."""
    assert _soft_delete_guard_tripped(delete_count=3, live_count=4) is False


def test_guard_floor_is_the_documented_constant() -> None:
    below = MIN_LIBRARY_FOR_RATIO_CHECK - 1
    assert _soft_delete_guard_tripped(delete_count=below, live_count=below) is False
    assert (
        _soft_delete_guard_tripped(
            delete_count=MIN_LIBRARY_FOR_RATIO_CHECK,
            live_count=MIN_LIBRARY_FOR_RATIO_CHECK,
        )
        is True
    )


def test_ratio_constant_is_the_documented_value() -> None:
    assert MAX_SOFT_DELETE_RATIO == 0.5
    assert MIN_LIBRARY_FOR_RATIO_CHECK == 20


async def _memory_session_maker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.mark.anyio
async def test_apply_soft_deletes_tombstones_without_hard_deleting_the_row() -> None:
    """The row survives so its metadata is reusable if the media comes back."""
    session_maker = await _memory_session_maker()
    async with session_maker() as db:
        series = Series(title="Series A", tmdb_id=1001, size=1)
        series.arr_added_at = datetime.now(UTC)
        db.add(series)
        await db.flush()
        row_id = series.id

        deleted_ids = await _apply_soft_deletes(db, [series], MediaType.SERIES)
        await db.commit()

        assert deleted_ids == [row_id]
        stored = (await db.execute(select(Series))).scalars().all()
        assert len(stored) == 1
        assert stored[0].removed_at is not None
        assert stored[0].added_at is None
        assert stored[0].arr_added_at is None


@pytest.mark.anyio
async def test_apply_soft_deletes_removes_derived_candidate_rows() -> None:
    session_maker = await _memory_session_maker()
    async with session_maker() as db:
        series = Series(title="Series A", tmdb_id=1001, size=1)
        db.add(series)
        await db.flush()

        candidate = ReclaimCandidate(
            media_type=MediaType.SERIES,
            matched_rule_ids=[1],
            matched_criteria={},
            reason="test",
        )
        candidate.series_id = series.id
        db.add(candidate)
        await db.commit()

        await _apply_soft_deletes(db, [series], MediaType.SERIES)
        await db.commit()

        assert (await db.execute(select(ReclaimCandidate))).scalars().all() == []


@pytest.mark.anyio
async def test_apply_soft_deletes_on_an_empty_set_is_a_no_op() -> None:
    session_maker = await _memory_session_maker()
    async with session_maker() as db:
        assert await _apply_soft_deletes(db, [], MediaType.SERIES) == []


@pytest.mark.anyio
async def test_apply_soft_deletes_targets_movies_by_movie_id() -> None:
    """The wrong foreign key would delete another medium's derived rows."""
    session_maker = await _memory_session_maker()
    async with session_maker() as db:
        movie = Movie(title="Movie A", tmdb_id=2001, size=1)
        unrelated = Series(title="Series A", tmdb_id=1001, size=1)
        db.add_all([movie, unrelated])
        await db.flush()

        series_candidate = ReclaimCandidate(
            media_type=MediaType.SERIES,
            matched_rule_ids=[1],
            matched_criteria={},
            reason="test",
        )
        series_candidate.series_id = unrelated.id
        db.add(series_candidate)
        await db.commit()

        await _apply_soft_deletes(db, [movie], MediaType.MOVIE)
        await db.commit()

        remaining = (await db.execute(select(ReclaimCandidate))).scalars().all()
        assert [c.series_id for c in remaining] == [unrelated.id]
        assert unrelated.removed_at is None


@pytest.mark.anyio
async def test_apply_soft_deletes_removes_protected_media_rows() -> None:
    """Today both manual and rule-sourced protection go with the row.

    Task 5 changes this policy; this test is the before-picture it changes
    against.
    """
    session_maker = await _memory_session_maker()
    async with session_maker() as db:
        series = Series(title="Series A", tmdb_id=1001, size=1)
        db.add(series)
        await db.flush()

        manual = ProtectedMedia(media_type=MediaType.SERIES)
        manual.series_id = series.id
        manual.source = "manual"
        rule = ProtectedMedia(media_type=MediaType.SERIES)
        rule.series_id = series.id
        rule.source = "rule"
        db.add_all([manual, rule])
        await db.commit()

        await _apply_soft_deletes(db, [series], MediaType.SERIES)
        await db.commit()

        assert (await db.execute(select(ProtectedMedia))).scalars().all() == []


@pytest.mark.anyio
async def test_apply_soft_deletes_removes_protection_request_rows() -> None:
    session_maker = await _memory_session_maker()
    async with session_maker() as db:
        user = User(username="someone", password_hash="x", role=UserRole.ADMIN)
        series = Series(title="Series A", tmdb_id=1001, size=1)
        db.add_all([user, series])
        await db.flush()

        request = ProtectionRequest(
            media_type=MediaType.SERIES, requested_by_user_id=user.id
        )
        request.series_id = series.id
        db.add(request)
        await db.commit()

        await _apply_soft_deletes(db, [series], MediaType.SERIES)
        await db.commit()

        assert (await db.execute(select(ProtectionRequest))).scalars().all() == []


@pytest.mark.anyio
async def test_apply_soft_deletes_leaves_other_medium_protection_alone() -> None:
    """The same shape as targeting by foreign key: an unrelated medium's
    protection rows must survive a soft-delete of a different medium.
    """
    session_maker = await _memory_session_maker()
    async with session_maker() as db:
        user = User(username="someone", password_hash="x", role=UserRole.ADMIN)
        movie = Movie(title="Movie A", tmdb_id=2001, size=1)
        unrelated = Series(title="Series A", tmdb_id=1001, size=1)
        db.add_all([user, movie, unrelated])
        await db.flush()

        protection = ProtectedMedia(media_type=MediaType.SERIES)
        protection.series_id = unrelated.id
        protection.source = "manual"
        request = ProtectionRequest(
            media_type=MediaType.SERIES, requested_by_user_id=user.id
        )
        request.series_id = unrelated.id
        db.add_all([protection, request])
        await db.commit()

        await _apply_soft_deletes(db, [movie], MediaType.MOVIE)
        await db.commit()

        remaining_protection = (
            (await db.execute(select(ProtectedMedia))).scalars().all()
        )
        remaining_requests = (
            (await db.execute(select(ProtectionRequest))).scalars().all()
        )
        assert [p.series_id for p in remaining_protection] == [unrelated.id]
        assert [r.series_id for r in remaining_requests] == [unrelated.id]
        assert unrelated.removed_at is None
