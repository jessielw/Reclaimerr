from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    GeneralSettings,
    Movie,
    MovieArrRef,
    MovieVersion,
    ReclaimCandidate,
    ReclaimRule,
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.tasks import cleanup


class FakeRadarr:
    def __init__(self) -> None:
        self.deleted: list[dict[str, Any]] = []
        self.unmonitored: list[list[int]] = []
        self.refreshed: list[list[int]] = []

    async def delete_movies(
        self,
        movie_ids: list[int],
        delete_files: bool = True,
        add_import_exclusion: bool = False,
    ) -> None:
        self.deleted.append(
            {
                "movie_ids": movie_ids,
                "delete_files": delete_files,
                "add_import_exclusion": add_import_exclusion,
            }
        )

    async def unmonitor_movies(self, movie_ids: list[int]) -> None:
        self.unmonitored.append(movie_ids)

    async def rescan_movies(self, movie_ids: list[int]) -> None:
        self.refreshed.append(movie_ids)


def _patch_services(
    monkeypatch,
    radarr: FakeRadarr | dict[int, FakeRadarr],
    config_id: int | None = None,
) -> None:
    clients = radarr if isinstance(radarr, dict) else {config_id: radarr}
    monkeypatch.setattr(cleanup.service_manager, "_radarr", None)
    monkeypatch.setattr(cleanup.service_manager, "_radarr_clients", clients)
    monkeypatch.setattr(cleanup.service_manager, "_main_media_server", None)
    monkeypatch.setattr(cleanup.service_manager, "_jellyfin", None)
    monkeypatch.setattr(cleanup.service_manager, "_emby", None)
    monkeypatch.setattr(cleanup.service_manager, "_plex", None)
    monkeypatch.setattr(cleanup.service_manager, "_seerr", None)


async def _make_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(cleanup, "async_db", session_maker)
    return engine, session_maker


async def _seed_movie_version_case(
    db: AsyncSession,
    *,
    version_paths: list[str],
    candidate_version_indexes: list[int],
    arr_action: str = "delete",
    media_server_fallback_enabled: bool = False,
    path_mappings: list[dict] | None = None,
    arr_movie_path: str | None = "/data/movies/Movie1",
    arr_movie_id: int = 55,
) -> tuple[int, list[int], int]:
    db.add(
        GeneralSettings(
            media_server_fallback_enabled=media_server_fallback_enabled,
            path_mappings=path_mappings or [],
        )
    )
    service_config = ServiceConfig(
        service_type=Service.RADARR,
        base_url="http://radarr",
        api_key="secret",
        name="Radarr",
        enabled=True,
    )
    movie = Movie(title="Movie1", tmdb_id=101, year=2020, size=0)
    rule = ReclaimRule(
        name="Rule",
        media_type=MediaType.MOVIE,
        enabled=True,
        target_scope="movie_version",
        definition={
            "version": 1,
            "root": {"type": "group", "op": "and", "children": []},
        },
        action={"candidate": True, "arr_action": arr_action},
    )
    db.add_all([service_config, movie, rule])
    await db.flush()

    versions: list[MovieVersion] = []
    for index, path in enumerate(version_paths, start=1):
        version = MovieVersion(
            movie_id=movie.id,
            service=Service.PLEX,
            service_item_id=f"item-{index}",
            service_media_id=f"media-{index}",
            library_id="lib-1",
            library_name="Movies",
            path=path,
            size=100,
        )
        versions.append(version)
        db.add(version)
    movie.size = len(versions) * 100
    db.add(
        MovieArrRef(
            movie_id=movie.id,
            service_config_id=service_config.id,
            arr_movie_id=arr_movie_id,
            arr_movie_path=arr_movie_path,
            tmdb_id=movie.tmdb_id,
        )
    )
    await db.flush()

    candidate_ids: list[int] = []
    for index in candidate_version_indexes:
        candidate = ReclaimCandidate(
            media_type=MediaType.MOVIE,
            matched_rule_ids=[rule.id],
            matched_criteria={},
            reason="cleanup",
            reason_data=[],
            movie_id=movie.id,
            movie_version_id=versions[index].id,
            estimated_space_bytes=versions[index].size,
        )
        db.add(candidate)
        await db.flush()
        candidate_ids.append(candidate.id)

    await db.commit()
    return movie.id, candidate_ids, service_config.id


def test_single_version_candidate_promotes_to_radarr_delete(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                movie_id, candidate_ids, config_id = await _seed_movie_version_case(
                    db,
                    version_paths=["/data/movies/Movie1/Movie1.mkv"],
                    candidate_version_indexes=[0],
                )

            radarr = FakeRadarr()
            _patch_services(monkeypatch, radarr, config_id)

            deleted = await cleanup._delete_movie_candidates(
                restrict_to_ids=frozenset(candidate_ids),
                approved_by="tester",
            )

            assert deleted == 1
            assert radarr.deleted == [
                {
                    "movie_ids": [55],
                    "delete_files": True,
                    "add_import_exclusion": True,
                }
            ]
            async with session_maker() as db:
                assert await db.get(ReclaimCandidate, candidate_ids[0]) is None
                movie = await db.get(Movie, movie_id)
                assert movie is not None
                assert movie.removed_at is not None
                versions = (await db.execute(select(MovieVersion))).scalars().all()
                assert versions == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_single_version_candidate_promotes_without_path_proof_for_one_radarr(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                _movie_id, candidate_ids, config_id = await _seed_movie_version_case(
                    db,
                    version_paths=["/plex/movies/Movie1/Movie1.mkv"],
                    candidate_version_indexes=[0],
                )

            radarr = FakeRadarr()
            _patch_services(monkeypatch, radarr, config_id)

            deleted = await cleanup._delete_movie_candidates(
                restrict_to_ids=frozenset(candidate_ids),
                approved_by="tester",
            )

            assert deleted == 1
            assert radarr.deleted == [
                {
                    "movie_ids": [55],
                    "delete_files": True,
                    "add_import_exclusion": True,
                }
            ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_partial_version_candidate_does_not_delete_unselected_versions(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                movie_id, candidate_ids, config_id = await _seed_movie_version_case(
                    db,
                    version_paths=[
                        "/data/movies/Movie1/Movie1-1080p.mkv",
                        "/data/movies/Movie1/Movie1-4k.mkv",
                    ],
                    candidate_version_indexes=[0],
                )

            radarr = FakeRadarr()
            _patch_services(monkeypatch, radarr, config_id)

            deleted = await cleanup._delete_movie_candidates(
                restrict_to_ids=frozenset(candidate_ids),
                approved_by="tester",
            )

            assert deleted == 0
            assert radarr.deleted == []
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_ids[0])
                assert candidate is not None
                assert candidate.delete_attempts == 1
                assert candidate.last_delete_error is not None
                assert "Partial movie-version delete requires" in (
                    candidate.last_delete_error
                )
                movie = await db.get(Movie, movie_id)
                assert movie is not None
                assert movie.removed_at is None
                versions = (await db.execute(select(MovieVersion))).scalars().all()
                assert len(versions) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_single_version_multi_radarr_uses_path_mapping_to_target_ref(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                movie_id, candidate_ids, config_id = await _seed_movie_version_case(
                    db,
                    version_paths=["/plex/movies/Movie1/Movie1.mkv"],
                    candidate_version_indexes=[0],
                    path_mappings=[
                        {
                            "source_prefix": "/plex/movies",
                            "local_prefix": "/library/movies",
                        },
                        {
                            "source_prefix": "/radarr1080",
                            "local_prefix": "/library/movies",
                        },
                        {
                            "source_prefix": "/radarr4k",
                            "local_prefix": "/library-4k",
                        },
                    ],
                    arr_movie_path="/radarr1080/Movie1",
                    arr_movie_id=55,
                )
                second_config = ServiceConfig(
                    service_type=Service.RADARR,
                    base_url="http://radarr-4k",
                    api_key="secret",
                    name="Radarr 4K",
                    enabled=True,
                )
                db.add(second_config)
                await db.flush()
                db.add(
                    MovieArrRef(
                        movie_id=movie_id,
                        service_config_id=second_config.id,
                        arr_movie_id=66,
                        arr_movie_path="/radarr4k/Movie1",
                        tmdb_id=101,
                    )
                )
                await db.commit()

            primary = FakeRadarr()
            secondary = FakeRadarr()
            _patch_services(
                monkeypatch,
                {config_id: primary, second_config.id: secondary},
            )

            deleted = await cleanup._delete_movie_candidates(
                restrict_to_ids=frozenset(candidate_ids),
                approved_by="tester",
            )

            assert deleted == 1
            assert primary.deleted == [
                {
                    "movie_ids": [55],
                    "delete_files": True,
                    "add_import_exclusion": True,
                }
            ]
            assert secondary.deleted == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_single_version_multi_radarr_without_path_proof_fails_closed(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                movie_id, candidate_ids, config_id = await _seed_movie_version_case(
                    db,
                    version_paths=["/plex/movies/Movie1/Movie1.mkv"],
                    candidate_version_indexes=[0],
                    arr_movie_path="/radarr1080/Movie1",
                    arr_movie_id=55,
                )
                second_config = ServiceConfig(
                    service_type=Service.RADARR,
                    base_url="http://radarr-4k",
                    api_key="secret",
                    name="Radarr 4K",
                    enabled=True,
                )
                db.add(second_config)
                await db.flush()
                db.add(
                    MovieArrRef(
                        movie_id=movie_id,
                        service_config_id=second_config.id,
                        arr_movie_id=66,
                        arr_movie_path="/radarr4k/Movie1",
                        tmdb_id=101,
                    )
                )
                await db.commit()

            primary = FakeRadarr()
            secondary = FakeRadarr()
            _patch_services(
                monkeypatch,
                {config_id: primary, second_config.id: secondary},
            )

            deleted = await cleanup._delete_movie_candidates(
                restrict_to_ids=frozenset(candidate_ids),
                approved_by="tester",
            )

            assert deleted == 0
            assert primary.deleted == []
            assert secondary.deleted == []
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_ids[0])
                assert candidate is not None
                assert candidate.delete_attempts == 1
                assert candidate.last_delete_error is not None
                assert "Radarr delete that covers the full Radarr movie entry" in (
                    candidate.last_delete_error
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_all_versions_selected_can_delete_via_radarr(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                movie_id, candidate_ids, config_id = await _seed_movie_version_case(
                    db,
                    version_paths=[
                        "/data/movies/Movie1/Movie1-1080p.mkv",
                        "/data/movies/Movie1/Movie1-4k.mkv",
                    ],
                    candidate_version_indexes=[0, 1],
                )

            radarr = FakeRadarr()
            _patch_services(monkeypatch, radarr, config_id)

            deleted = await cleanup._delete_movie_candidates(
                restrict_to_ids=frozenset(candidate_ids),
                approved_by="tester",
            )

            assert deleted == 1
            assert radarr.deleted == [
                {
                    "movie_ids": [55],
                    "delete_files": True,
                    "add_import_exclusion": True,
                }
            ]
            async with session_maker() as db:
                assert (
                    await db.execute(select(ReclaimCandidate))
                ).scalars().all() == []
                movie = await db.get(Movie, movie_id)
                assert movie is not None
                assert movie.removed_at is not None
                assert (await db.execute(select(MovieVersion))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unmonitor_action_still_deletes_selected_file_locally(
    monkeypatch, tmp_path: Path
) -> None:
    async def run() -> None:
        movie_dir = tmp_path / "movies" / "Movie1"
        movie_dir.mkdir(parents=True)
        media_file = movie_dir / "Movie1.mkv"
        media_file.write_bytes(b"movie")

        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                movie_id, candidate_ids, config_id = await _seed_movie_version_case(
                    db,
                    version_paths=["/data/movies/Movie1/Movie1.mkv"],
                    candidate_version_indexes=[0],
                    arr_action="unmonitor",
                    path_mappings=[
                        {
                            "source_prefix": "/data",
                            "local_prefix": str(tmp_path).replace("\\", "/"),
                        }
                    ],
                )

            radarr = FakeRadarr()
            _patch_services(monkeypatch, radarr, config_id)

            deleted = await cleanup._delete_movie_candidates(
                restrict_to_ids=frozenset(candidate_ids),
                approved_by="tester",
            )

            assert deleted == 1
            assert radarr.deleted == []
            assert radarr.unmonitored == [[55]]
            assert not media_file.exists()
            async with session_maker() as db:
                assert await db.get(ReclaimCandidate, candidate_ids[0]) is None
                movie = await db.get(Movie, movie_id)
                assert movie is not None
                assert movie.removed_at is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
