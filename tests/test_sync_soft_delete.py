from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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
    _previous_large_delete_sets,
    _select_rows_to_soft_delete,
    _soft_delete_blocked,
    _soft_delete_guard_tripped,
)


@pytest.fixture(autouse=True)
def _clear_remembered_delete_sets():
    """The two-strikes memory is module state; do not leak it between tests."""
    _previous_large_delete_sets.clear()
    yield
    _previous_large_delete_sets.clear()


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


def _large_delete_set(first_id: int = 1) -> list[Series]:
    """Over-ratio against a live count of 100, and past the small-library floor."""
    return [_series(row_id, 1000 + row_id) for row_id in range(first_id, first_id + 60)]


def test_the_same_large_delete_set_is_allowed_on_the_second_run() -> None:
    """Blocking on the ratio alone never relents after a main-server switch.

    The phantom rows the pass wants gone are themselves counted as live, so the
    delete set stays over the ratio every run and the pass is skipped forever.
    A set proposed twice is a real reduction, not a flapping server.
    """
    rows = _large_delete_set()

    assert _soft_delete_blocked(MediaType.SERIES, rows, live_count=100) is True
    assert _soft_delete_blocked(MediaType.SERIES, rows, live_count=100) is False


def test_a_different_large_delete_set_each_run_stays_blocked() -> None:
    """The case the guard is actually for: a server answering inconsistently."""
    assert (
        _soft_delete_blocked(MediaType.SERIES, _large_delete_set(1), live_count=100)
        is True
    )
    assert (
        _soft_delete_blocked(MediaType.SERIES, _large_delete_set(500), live_count=100)
        is True
    )
    assert (
        _soft_delete_blocked(MediaType.SERIES, _large_delete_set(900), live_count=100)
        is True
    )


def test_a_healthy_run_clears_the_remembered_set() -> None:
    """A sub-threshold run means the earlier set was never confirmed."""
    rows = _large_delete_set()

    assert _soft_delete_blocked(MediaType.SERIES, rows, live_count=100) is True
    assert _soft_delete_blocked(MediaType.SERIES, rows[:1], live_count=100) is False
    assert _previous_large_delete_sets == {}
    assert _soft_delete_blocked(MediaType.SERIES, rows, live_count=100) is True


def test_the_two_media_types_are_remembered_separately() -> None:
    """A movie pass must not confirm a series pass, or vice versa."""
    series_rows = _large_delete_set()
    movie_rows = [_movie(row_id, 2000 + row_id) for row_id in range(1, 61)]

    assert _soft_delete_blocked(MediaType.SERIES, series_rows, live_count=100) is True
    assert _soft_delete_blocked(MediaType.MOVIE, movie_rows, live_count=100) is True
    assert _soft_delete_blocked(MediaType.SERIES, series_rows, live_count=100) is False
    assert _soft_delete_blocked(MediaType.MOVIE, movie_rows, live_count=100) is False


def test_set_membership_not_ordering_decides_the_repeat() -> None:
    """Row order comes from the media server and carries no meaning."""
    rows = _large_delete_set()

    assert _soft_delete_blocked(MediaType.SERIES, rows, live_count=100) is True
    assert (
        _soft_delete_blocked(MediaType.SERIES, list(reversed(rows)), live_count=100)
        is False
    )


async def _memory_session_maker() -> tuple[
    async_sessionmaker[AsyncSession], AsyncEngine
]:
    """Returns the session maker and the engine backing it.

    The caller must dispose the engine, otherwise the aiosqlite worker thread
    outlives the event loop and pytest reports an unhandled thread exception in
    the warnings summary.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    return session_maker, engine


@pytest.mark.anyio
async def test_apply_soft_deletes_tombstones_without_hard_deleting_the_row() -> None:
    """The row survives so its metadata is reusable if the media comes back."""
    session_maker, engine = await _memory_session_maker()
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
    await engine.dispose()


@pytest.mark.anyio
async def test_apply_soft_deletes_removes_derived_candidate_rows() -> None:
    session_maker, engine = await _memory_session_maker()
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
    await engine.dispose()


@pytest.mark.anyio
async def test_apply_soft_deletes_on_an_empty_set_is_a_no_op() -> None:
    session_maker, engine = await _memory_session_maker()
    async with session_maker() as db:
        assert await _apply_soft_deletes(db, [], MediaType.SERIES) == []
    await engine.dispose()


@pytest.mark.anyio
async def test_apply_soft_deletes_targets_movies_by_movie_id() -> None:
    """The wrong foreign key would delete another medium's derived rows."""
    session_maker, engine = await _memory_session_maker()
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
    await engine.dispose()


@pytest.mark.anyio
async def test_apply_soft_deletes_removes_protected_media_rows() -> None:
    """Only rule-sourced protection is removed; it regenerates from the rule task.

    Manual protection is a person's decision and nothing can rebuild it, so it
    must survive.
    """
    session_maker, engine = await _memory_session_maker()
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

        remaining = (await db.execute(select(ProtectedMedia))).scalars().all()
        assert [p.source for p in remaining] == ["manual"]
    await engine.dispose()


@pytest.mark.anyio
async def test_apply_soft_deletes_leaves_protection_request_rows_alone() -> None:
    """A protection request is a human decision that nothing can rebuild."""
    session_maker, engine = await _memory_session_maker()
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

        assert len((await db.execute(select(ProtectionRequest))).scalars().all()) == 1
    await engine.dispose()


@pytest.mark.anyio
async def test_apply_soft_deletes_leaves_other_medium_protection_alone() -> None:
    """The same shape as targeting by foreign key: an unrelated medium's
    protection rows must survive a soft-delete of a different medium.
    """
    session_maker, engine = await _memory_session_maker()
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
    await engine.dispose()


def _protection(series_id: int, source: str) -> ProtectedMedia:
    protection = ProtectedMedia(media_type=MediaType.SERIES)
    protection.series_id = series_id
    protection.source = source
    return protection


@pytest.mark.anyio
async def test_manual_protection_survives_a_tombstone() -> None:
    """A person's decision cannot be reconstructed, so it must not be deleted.

    The medium is only soft-deleted and is restored if it reappears. Hard-deleting
    the protection meant that restore came back silently unprotected.
    """
    session_maker, engine = await _memory_session_maker()
    async with session_maker() as db:
        series = Series(title="Series A", tmdb_id=1001, size=1)
        db.add(series)
        await db.flush()
        db.add_all([_protection(series.id, "manual"), _protection(series.id, "rule")])
        await db.commit()

        await _apply_soft_deletes(db, [series], MediaType.SERIES)
        await db.commit()

        remaining = (await db.execute(select(ProtectedMedia))).scalars().all()
        assert [p.source for p in remaining] == ["manual"]
    await engine.dispose()


@pytest.mark.anyio
async def test_protection_request_survives_a_tombstone() -> None:
    session_maker, engine = await _memory_session_maker()
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

        assert len((await db.execute(select(ProtectionRequest))).scalars().all()) == 1
    await engine.dispose()


@pytest.mark.anyio
async def test_manual_protection_is_still_attached_after_a_restore() -> None:
    """The point of preserving it: the protection must mean something again.

    A restore is just clearing removed_at, which is what sync_series does when
    the media reappears.
    """
    session_maker, engine = await _memory_session_maker()
    async with session_maker() as db:
        series = Series(title="Series A", tmdb_id=1001, size=1)
        db.add(series)
        await db.flush()
        db.add(_protection(series.id, "manual"))
        await db.commit()

        await _apply_soft_deletes(db, [series], MediaType.SERIES)
        await db.commit()
        series.removed_at = None
        await db.commit()

        protections = (await db.execute(select(ProtectedMedia))).scalars().all()
        assert [p.series_id for p in protections] == [series.id]
    await engine.dispose()
