from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.requests import create_protection_request
from backend.database import Base
from backend.database.models import (
    Movie,
    MovieArrRef,
    ReclaimCandidate,
    ReclaimRule,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, ProtectionRequestStatus, Service, UserRole
from backend.models.requests import CreateProtectionRequest
from backend.tasks import cleanup


class TaggingRadarr:
    """Radarr double holding one tagged candidate and one tagged non-candidate."""

    def __init__(self, movies: list[SimpleNamespace]) -> None:
        self.movies = movies
        self.catalog = [SimpleNamespace(id=5, label="rec-clean")]
        self.added: list[tuple[list[int], int]] = []
        self.removed: list[tuple[list[int], int]] = []
        self.get_all_movies_calls = 0

    async def get_all_movies(self) -> list[SimpleNamespace]:
        self.get_all_movies_calls += 1
        return self.movies

    async def get_movie(self, movie_id: int) -> SimpleNamespace:
        for movie in self.movies:
            if movie.id == movie_id:
                return movie
        raise ValueError(f"movie {movie_id} not found")

    async def get_tags(self) -> list[SimpleNamespace]:
        return self.catalog

    async def get_or_create_tag(self, _label: str) -> SimpleNamespace:
        return SimpleNamespace(id=5, label="rec-clean")

    async def add_tag_to_movies(self, movie_ids: list[int], tag_id: int) -> None:
        self.added.append((movie_ids, tag_id))

    async def remove_tag_from_movies(self, movie_ids: list[int], tag_id: int) -> None:
        self.removed.append((movie_ids, tag_id))


async def _seed(session_maker) -> SimpleNamespace:
    """One tagging rule, one movie still matching it and one that dropped out."""
    async with session_maker() as db:
        config = ServiceConfig(
            service_type=Service.RADARR,
            base_url="http://radarr",
            api_key="secret",
            name="Radarr",
            enabled=True,
        )
        candidate_movie = Movie(title="Still Matching", tmdb_id=101, size=100)
        dropped_movie = Movie(title="No Longer Matching", tmdb_id=102, size=100)
        rule = ReclaimRule(
            name="Clean",
            media_type=MediaType.MOVIE,
            enabled=True,
            target_scope="movie_version",
            definition={
                "version": 1,
                "root": {"type": "group", "op": "and", "children": []},
            },
            action={"tag_enabled": True, "arr_tag": "rec-clean"},
        )
        db.add_all([config, candidate_movie, dropped_movie, rule])
        await db.flush()
        rule.action = {**(rule.action or {}), "radarr_service_config_ids": [config.id]}
        db.add_all(
            [
                MovieArrRef(
                    movie_id=candidate_movie.id,
                    service_config_id=config.id,
                    arr_movie_id=51,
                    tmdb_id=candidate_movie.tmdb_id,
                ),
                MovieArrRef(
                    movie_id=dropped_movie.id,
                    service_config_id=config.id,
                    arr_movie_id=52,
                    tmdb_id=dropped_movie.tmdb_id,
                ),
                ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    matched_rule_ids=[rule.id],
                    matched_criteria={},
                    reason="cleanup",
                    reason_data=[],
                    movie_id=candidate_movie.id,
                ),
            ]
        )
        await db.commit()
        return SimpleNamespace(
            candidate_movie_id=candidate_movie.id,
            dropped_movie_id=dropped_movie.id,
            rule_id=rule.id,
            config_id=config.id,
        )


def _radarr_double(monkeypatch, session_maker) -> TaggingRadarr:
    client = TaggingRadarr(
        [
            SimpleNamespace(id=51, tmdb_id=101, tags=[5]),
            SimpleNamespace(id=52, tmdb_id=102, tags=[5]),
        ]
    )
    monkeypatch.setattr(cleanup, "async_db", session_maker)
    monkeypatch.setattr(cleanup.service_manager, "_radarr", client)
    monkeypatch.setattr(cleanup.service_manager, "_radarr_clients", {1: client})
    return client


def test_scan_removes_managed_tag_from_dropped_candidate(monkeypatch) -> None:
    """A scan that drops a candidate must strip its managed tag immediately."""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        await _seed(session_maker)
        client = _radarr_double(monkeypatch, session_maker)
        monkeypatch.setattr("backend.core.task_tracking.async_db", session_maker)

        async def fake_scan(_db) -> tuple[int, int, int]:
            return 0, 0, 1

        monkeypatch.setattr(cleanup, "_scan_with_db", fake_scan)

        await cleanup.scan_cleanup_candidates()

        assert client.removed == [([52], 5)]
        assert client.added == []
        await engine.dispose()

    asyncio.run(run())


def test_tag_reconciliation_failure_does_not_fail_the_scan(monkeypatch) -> None:
    """Arr tagging is best effort; a failing client must not abort the scan."""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        await _seed(session_maker)
        client = _radarr_double(monkeypatch, session_maker)
        monkeypatch.setattr("backend.core.task_tracking.async_db", session_maker)

        async def boom() -> list[SimpleNamespace]:
            raise RuntimeError("radarr unreachable")

        monkeypatch.setattr(client, "get_all_movies", boom)

        async def fake_scan(_db) -> tuple[int, int, int]:
            return 0, 0, 1

        monkeypatch.setattr(cleanup, "_scan_with_db", fake_scan)

        await cleanup.scan_cleanup_candidates()

        assert client.removed == []
        await engine.dispose()

    asyncio.run(run())


def test_protecting_one_title_drops_its_managed_tag(monkeypatch) -> None:
    """Approving a protection untags that one item without a full arr sweep."""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        ids = await _seed(session_maker)
        client = _radarr_double(monkeypatch, session_maker)

        async with session_maker() as db:
            # what an approval does: protect the title and drop its candidate
            await db.execute(
                delete(ReclaimCandidate).where(
                    ReclaimCandidate.movie_id == ids.candidate_movie_id
                )
            )
            await db.commit()

            await cleanup.drop_managed_arr_tags_for_media(
                db,
                media_type=MediaType.MOVIE,
                movie_id=ids.candidate_movie_id,
            )

        assert client.removed == [([51], 5)]
        # the whole library was never fetched
        assert client.get_all_movies_calls == 0
        await engine.dispose()

    asyncio.run(run())


def test_targeted_untag_keeps_a_tag_another_candidate_still_earns(monkeypatch) -> None:
    """A surviving candidate for the same title keeps the tag in place."""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        ids = await _seed(session_maker)
        client = _radarr_double(monkeypatch, session_maker)

        async with session_maker() as db:
            await cleanup.drop_managed_arr_tags_for_media(
                db,
                media_type=MediaType.MOVIE,
                movie_id=ids.candidate_movie_id,
            )

        assert client.removed == []
        await engine.dispose()

    asyncio.run(run())


def test_targeted_untag_leaves_tags_the_user_owns(monkeypatch) -> None:
    """Only Reclaimerr's own tag prefixes are ever taken off an item."""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        ids = await _seed(session_maker)
        client = _radarr_double(monkeypatch, session_maker)
        client.movies[1].tags = [5, 9]
        client.catalog = [
            SimpleNamespace(id=5, label="rec-clean"),
            SimpleNamespace(id=9, label="keep-forever"),
        ]

        async with session_maker() as db:
            await cleanup.drop_managed_arr_tags_for_media(
                db,
                media_type=MediaType.MOVIE,
                movie_id=ids.dropped_movie_id,
            )

        assert client.removed == [([52], 5)]
        await engine.dispose()

    asyncio.run(run())


def test_auto_approved_protection_untags_the_movie(monkeypatch) -> None:
    """The approval handler itself has to take the tag off, not just the scan."""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        ids = await _seed(session_maker)
        client = _radarr_double(monkeypatch, session_maker)

        async with session_maker() as db:
            admin = User(
                username="admin",
                password_hash="x",
                role=UserRole.ADMIN,
                permissions=[],
            )
            db.add(admin)
            await db.commit()

            response = await create_protection_request(
                CreateProtectionRequest(
                    media_type=MediaType.MOVIE,
                    media_id=ids.candidate_movie_id,
                    reason="Keep this one",
                ),
                admin,
                db,
            )

            assert response.status is ProtectionRequestStatus.APPROVED

        assert client.removed == [([51], 5)]
        await engine.dispose()

    asyncio.run(run())
