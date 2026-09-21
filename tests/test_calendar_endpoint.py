from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.calendar import MAX_WINDOW_DAYS, get_calendar
from backend.database import Base
from backend.database.models import (
    Episode,
    Movie,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    User,
)
from backend.enums import MediaType, UserRole

# Deadlines are resolved against the real clock, so every fixture is timed
# relative to it. Noon keeps the UTC-offset test's expected day unambiguous.
BASE = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
DELAY_DAYS = 10
DEADLINE = BASE + timedelta(days=DELAY_DAYS)
WINDOW_START = (BASE - timedelta(days=1)).date()
WINDOW_END = (BASE + timedelta(days=90)).date()


def _user() -> User:
    return User(username="admin", password_hash="x", role=UserRole.ADMIN)


def _rule(rule_id: int, **action: object) -> ReclaimRule:
    rule = ReclaimRule(
        name=f"rule-{rule_id}",
        media_type=MediaType.MOVIE,
        enabled=True,
        target_scope="movie_version",
        definition=None,
        action={
            "auto_delete_enabled": True,
            "auto_delete_delay_days": DELAY_DAYS,
            **action,
        },
    )
    rule.id = rule_id
    return rule


def _candidate(
    *,
    rule_ids: list[int],
    movie_id: int | None = None,
    series_id: int | None = None,
    season_id: int | None = None,
    episode_id: int | None = None,
    size: int | None = None,
) -> ReclaimCandidate:
    candidate = ReclaimCandidate(
        media_type=MediaType.MOVIE if movie_id else MediaType.SERIES,
        matched_rule_ids=rule_ids,
        matched_criteria={},
        reason="test",
        movie_id=movie_id,
        series_id=series_id,
        season_id=season_id,
        episode_id=episode_id,
    )
    candidate.created_at = BASE
    candidate.estimated_space_bytes = size
    return candidate


async def _session() -> tuple[AsyncSession, object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    return session_maker(), engine


@pytest.mark.anyio
async def test_groups_candidates_onto_their_deadline_day() -> None:
    db, engine = await _session()
    async with db:
        db.add_all(
            [
                _rule(1),
                Movie(title="Arrival", tmdb_id=1, size=500),
                _candidate(rule_ids=[1], movie_id=1, size=900),
            ]
        )
        await db.flush()

        response = await get_calendar(
            _user=_user(), start=WINDOW_START, end=WINDOW_END, db=db
        )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    assert [day.date for day in response.days] == [DEADLINE.date()]
    day = response.days[0]
    assert day.item_count == 1
    assert day.total_bytes == 900
    assert day.truncated is False
    assert day.items[0].title == "Arrival"
    assert day.items[0].scope == "movie"
    assert day.items[0].operation == "delete"
    assert response.total_items == 1
    assert response.total_bytes == 900


@pytest.mark.anyio
async def test_a_window_that_excludes_the_deadline_returns_nothing() -> None:
    db, engine = await _session()
    async with db:
        db.add_all(
            [_rule(1), Movie(title="Arrival", tmdb_id=1), _candidate(rule_ids=[1], movie_id=1)]
        )
        await db.flush()

        response = await get_calendar(
            _user=_user(),
            start=(BASE + timedelta(days=200)).date(),
            end=(BASE + timedelta(days=260)).date(),
            db=db,
        )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    assert response.days == []
    assert response.total_items == 0


@pytest.mark.anyio
async def test_a_cancelled_candidate_is_not_plotted() -> None:
    db, engine = await _session()
    async with db:
        cancelled = _candidate(rule_ids=[1], movie_id=1)
        cancelled.auto_delete_cancelled_at = BASE
        db.add_all([_rule(1), Movie(title="Arrival", tmdb_id=1), cancelled])
        await db.flush()

        response = await get_calendar(
            _user=_user(), start=WINDOW_START, end=WINDOW_END, db=db
        )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    assert response.total_items == 0


@pytest.mark.anyio
async def test_a_rule_without_automatic_deletion_is_not_plotted() -> None:
    db, engine = await _session()
    async with db:
        rule = _rule(1)
        rule.action = {"auto_delete_enabled": False}
        db.add_all(
            [rule, Movie(title="Arrival", tmdb_id=1), _candidate(rule_ids=[1], movie_id=1)]
        )
        await db.flush()

        response = await get_calendar(
            _user=_user(), start=WINDOW_START, end=WINDOW_END, db=db
        )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    assert response.total_items == 0


@pytest.mark.anyio
async def test_a_postponement_moves_the_item_to_its_new_day() -> None:
    db, engine = await _session()
    async with db:
        postponed = _candidate(rule_ids=[1], movie_id=1)
        postponed.auto_delete_postponed_until = BASE + timedelta(days=40)
        db.add_all([_rule(1), Movie(title="Arrival", tmdb_id=1), postponed])
        await db.flush()

        response = await get_calendar(
            _user=_user(), start=WINDOW_START, end=WINDOW_END, db=db
        )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    assert [day.date for day in response.days] == [(BASE + timedelta(days=40)).date()]
    assert response.days[0].items[0].state == "postponed"


@pytest.mark.anyio
async def test_a_move_rule_is_labelled_as_a_move() -> None:
    db, engine = await _session()
    async with db:
        db.add_all(
            [
                _rule(1, move_instead_of_delete=True),
                Movie(title="Arrival", tmdb_id=1),
                _candidate(rule_ids=[1], movie_id=1),
            ]
        )
        await db.flush()

        response = await get_calendar(
            _user=_user(), start=WINDOW_START, end=WINDOW_END, db=db
        )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    assert response.days[0].items[0].operation == "move"


@pytest.mark.anyio
async def test_an_episode_candidate_is_labelled_with_its_season_and_episode() -> None:
    db, engine = await _session()
    async with db:
        series = Series(title="Severance", tmdb_id=9)
        series.id = 1
        season = Season(series_id=1, season_number=2)
        season.id = 1
        episode = Episode(season_id=1, episode_number=7)
        episode.id = 1
        episode.size = 1234
        db.add_all(
            [
                _rule(1),
                series,
                season,
                episode,
                _candidate(rule_ids=[1], series_id=1, season_id=1, episode_id=1),
            ]
        )
        await db.flush()

        response = await get_calendar(
            _user=_user(), start=WINDOW_START, end=WINDOW_END, db=db
        )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    item = response.days[0].items[0]
    assert item.title == "Severance S02E07"
    assert item.scope == "episode"
    # the candidate carries no estimate, so the episode's own size stands in
    assert item.estimated_space_bytes == 1234


@pytest.mark.anyio
async def test_the_offset_decides_which_local_day_an_item_lands_on() -> None:
    db, engine = await _session()
    async with db:
        # the deadline is at noon UTC, so UTC+13 puts it on the following day
        db.add_all(
            [_rule(1), Movie(title="Arrival", tmdb_id=1), _candidate(rule_ids=[1], movie_id=1)]
        )
        await db.flush()

        shifted = await get_calendar(
            _user=_user(),
            start=WINDOW_START,
            end=WINDOW_END,
            db=db,
            tz_offset_minutes=13 * 60,
        )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    assert [day.date for day in shifted.days] == [
        (DEADLINE + timedelta(days=1)).date()
    ]


@pytest.mark.anyio
async def test_a_busy_day_reports_a_full_count_but_truncates_its_items() -> None:
    db, engine = await _session()
    async with db:
        rows: list[object] = [_rule(1)]
        for index in range(5):
            movie = Movie(title=f"Movie {index}", tmdb_id=index + 1)
            movie.id = index + 1
            rows.append(movie)
            rows.append(_candidate(rule_ids=[1], movie_id=index + 1, size=100))
        db.add_all(rows)  # pyright: ignore[reportArgumentType]
        await db.flush()

        response = await get_calendar(
            _user=_user(),
            start=WINDOW_START,
            end=WINDOW_END,
            db=db,
            per_day_limit=2,
        )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    day = response.days[0]
    assert day.item_count == 5
    assert day.total_bytes == 500
    assert len(day.items) == 2
    assert day.truncated is True


@pytest.mark.anyio
async def test_an_inverted_window_is_rejected() -> None:
    db, engine = await _session()
    async with db:
        with pytest.raises(HTTPException) as excinfo:
            await get_calendar(
                _user=_user(), start=WINDOW_END, end=WINDOW_START, db=db
            )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    assert excinfo.value.status_code == 400


@pytest.mark.anyio
async def test_an_oversized_window_is_rejected() -> None:
    db, engine = await _session()
    async with db:
        with pytest.raises(HTTPException) as excinfo:
            await get_calendar(
                _user=_user(),
                start=date(2026, 1, 1),
                end=date(2026, 1, 1) + timedelta(days=MAX_WINDOW_DAYS),
                db=db,
            )
    await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    assert excinfo.value.status_code == 400
