"""Regression tests for why a manual candidate deletion failed.

A candidate that no scoped handler can act on used to fall through the dispatch
in `_delete_specific_candidates_impl` and surface as a bare
"0 processed, 1 failed" with nothing above it in the log, which made the failure
undiagnosable from the outside. These cover the reasons now being named.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    GeneralSettings,
    Movie,
    MovieArrRef,
    MovieVersion,
    ReclaimCandidate,
    Series,
    SeriesArrRef,
    SeriesServiceRef,
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.tasks import cleanup


class FakeRadarr:
    def __init__(self) -> None:
        self.deleted: list[dict[str, Any]] = []

    async def delete_movies(
        self,
        movie_ids: list[int],
        delete_files: bool = True,
        add_import_exclusion: bool = False,
    ) -> None:
        self.deleted.append({"movie_ids": movie_ids})

    async def rescan_movies(self, movie_ids: list[int]) -> None:
        pass

    async def unmonitor_movies(self, movie_ids: list[int]) -> None:
        pass


class FakeSonarr:
    def __init__(self) -> None:
        self.deleted_series: list[int] = []

    async def delete_series(
        self,
        series_id: int,
        delete_files: bool = False,
        add_import_exclusion: bool = False,
    ) -> None:
        self.deleted_series.append(series_id)

    async def unmonitor_series(self, series_ids: list[int]) -> None:
        pass

    async def refresh_series(self, series_ids: list[int]) -> None:
        pass


class TimingOutSonarr(FakeSonarr):
    async def unmonitor_series(self, series_ids: list[int]) -> None:
        raise TimeoutError()


class FailingMediaServer:
    async def delete_item(self, item_id: str) -> None:
        raise ValueError(
            f"Failed to delete Plex item {item_id} (status=500, method=DELETE, "
            'body={"error":"library is locked"})'
        )


def _patch_services(
    monkeypatch,
    *,
    radarr: FakeRadarr | None = None,
    sonarr: FakeSonarr | None = None,
    main_media_server: object | None = None,
) -> None:
    monkeypatch.setattr(cleanup.service_manager, "_radarr", None)
    monkeypatch.setattr(
        cleanup.service_manager, "_radarr_clients", {1: radarr} if radarr else {}
    )
    monkeypatch.setattr(cleanup.service_manager, "_sonarr", None)
    monkeypatch.setattr(
        cleanup.service_manager, "_sonarr_clients", {1: sonarr} if sonarr else {}
    )
    monkeypatch.setattr(
        cleanup.service_manager, "_main_media_server", main_media_server
    )
    monkeypatch.setattr(cleanup.service_manager, "_jellyfin", None)
    monkeypatch.setattr(cleanup.service_manager, "_emby", None)
    monkeypatch.setattr(cleanup.service_manager, "_plex", None)
    monkeypatch.setattr(cleanup.service_manager, "_seerr", None)
    monkeypatch.setattr(cleanup.service_manager, "_seerr_clients", {})


async def _make_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(cleanup, "async_db", session_maker)
    return engine, session_maker


async def _seed_movie_candidate(
    db: AsyncSession,
    *,
    removed_at: datetime | None = None,
    last_delete_error: str | None = None,
) -> int:
    db.add(GeneralSettings(media_server_fallback_enabled=False, path_mappings=[]))
    service_config = ServiceConfig(
        service_type=Service.RADARR,
        base_url="http://radarr",
        api_key="secret",
        name="Radarr",
        enabled=True,
    )
    movie = Movie(title="Movie1", tmdb_id=101, year=2020, size=100)
    movie.removed_at = removed_at
    db.add_all([service_config, movie])
    await db.flush()
    db.add(
        MovieArrRef(
            movie_id=movie.id,
            service_config_id=service_config.id,
            arr_movie_id=55,
            arr_movie_path="/data/movies/Movie1",
            tmdb_id=movie.tmdb_id,
        )
    )
    candidate = ReclaimCandidate(
        media_type=MediaType.MOVIE,
        matched_rule_ids=[],
        matched_criteria={},
        reason="cleanup",
        reason_data=[],
        movie_id=movie.id,
        estimated_space_bytes=100,
    )
    candidate.last_delete_error = last_delete_error
    db.add(candidate)
    await db.flush()
    await db.commit()
    return candidate.id


async def _seed_series_candidate(db: AsyncSession, *, removed_at: datetime) -> int:
    db.add(GeneralSettings(media_server_fallback_enabled=False, path_mappings=[]))
    series = Series(title="Show1", tmdb_id=201, year=2020, size=100)
    series.removed_at = removed_at
    db.add(series)
    await db.flush()
    candidate = ReclaimCandidate(
        media_type=MediaType.SERIES,
        matched_rule_ids=[],
        matched_criteria={},
        reason="cleanup",
        reason_data=[],
        series_id=series.id,
        estimated_space_bytes=100,
    )
    db.add(candidate)
    await db.flush()
    await db.commit()
    return candidate.id


async def _seed_live_series_candidate(
    db: AsyncSession,
    *,
    sonarr_series_id: int | None,
    plex_item_id: str | None,
    sonarr_path: str | None = None,
) -> int:
    db.add(GeneralSettings(media_server_fallback_enabled=True, path_mappings=[]))
    service_config = ServiceConfig(
        service_type=Service.SONARR,
        base_url="http://sonarr",
        api_key="secret",
        name="Sonarr",
        enabled=True,
    )
    series = Series(title="Show1", tmdb_id=201, year=2020, size=100)
    db.add_all([service_config, series])
    await db.flush()
    if sonarr_series_id is not None:
        db.add(
            SeriesArrRef(
                series_id=series.id,
                service_config_id=service_config.id,
                arr_series_id=sonarr_series_id,
                arr_series_path=sonarr_path,
                tmdb_id=series.tmdb_id,
            )
        )
    if plex_item_id is not None:
        db.add(
            SeriesServiceRef(
                series_id=series.id,
                service=Service.PLEX,
                service_id=plex_item_id,
                library_id="2",
                library_name="TV",
            )
        )
    candidate = ReclaimCandidate(
        media_type=MediaType.SERIES,
        matched_rule_ids=[],
        matched_criteria={},
        reason="cleanup",
        reason_data=[],
        series_id=series.id,
        estimated_space_bytes=100,
    )
    db.add(candidate)
    await db.flush()
    await db.commit()
    return candidate.id


async def _seed_live_movie_candidate(
    db: AsyncSession, *, radarr_movie_id: int | None, plex_path: str
) -> int:
    db.add(GeneralSettings(media_server_fallback_enabled=True, path_mappings=[]))
    service_config = ServiceConfig(
        service_type=Service.RADARR,
        base_url="http://radarr",
        api_key="secret",
        name="Radarr",
        enabled=True,
    )
    movie = Movie(title="Movie1", tmdb_id=101, year=2020, size=100)
    db.add_all([service_config, movie])
    await db.flush()
    if radarr_movie_id is not None:
        db.add(
            MovieArrRef(
                movie_id=movie.id,
                service_config_id=service_config.id,
                arr_movie_id=radarr_movie_id,
                arr_movie_path=plex_path.rsplit("/", 1)[0],
                tmdb_id=movie.tmdb_id,
            )
        )
    db.add(
        MovieVersion(
            movie_id=movie.id,
            service=Service.PLEX,
            service_item_id="88",
            service_media_id="880",
            library_id="1",
            library_name="Movies",
            path=plex_path,
        )
    )
    candidate = ReclaimCandidate(
        media_type=MediaType.MOVIE,
        matched_rule_ids=[],
        matched_criteria={},
        reason="cleanup",
        reason_data=[],
        movie_id=movie.id,
        estimated_space_bytes=100,
    )
    db.add(candidate)
    await db.flush()
    await db.commit()
    return candidate.id


def test_tombstoned_movie_candidate_reports_stale_reason(monkeypatch, caplog) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_movie_candidate(
                    db, removed_at=datetime.now(UTC)
                )

            radarr = FakeRadarr()
            _patch_services(monkeypatch, radarr=radarr)

            with caplog.at_level(logging.WARNING, logger="reclaimerr"):
                deleted, failed = await cleanup.delete_specific_candidates(
                    [candidate_id], approved_by="tester"
                )

            assert (deleted, failed) == (0, 1)
            assert radarr.deleted == []
            # the reason has to reach the log, not only the candidate row
            assert any(
                "tombstoned in Reclaimerr" in record.getMessage()
                for record in caplog.records
            )
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.last_delete_error is not None
                assert "tombstoned in Reclaimerr" in candidate.last_delete_error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_tombstoned_series_candidate_reports_stale_reason(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_series_candidate(
                    db, removed_at=datetime.now(UTC)
                )

            sonarr = FakeSonarr()
            _patch_services(monkeypatch, sonarr=sonarr)

            deleted, failed = await cleanup.delete_specific_candidates(
                [candidate_id], approved_by="tester"
            )

            assert (deleted, failed) == (0, 1)
            assert sonarr.deleted_series == []
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.last_delete_error is not None
                assert "tombstoned in Reclaimerr" in candidate.last_delete_error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_movie_route_is_logged_and_recorded(monkeypatch, caplog) -> None:
    """No Radarr and no main media server used to skip the delete silently."""

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_movie_candidate(db)

            _patch_services(monkeypatch)

            with caplog.at_level(logging.ERROR, logger="reclaimerr"):
                deleted, failed = await cleanup.delete_specific_candidates(
                    [candidate_id], approved_by="tester"
                )

            assert (deleted, failed) == (0, 1)
            assert any(
                "No delete route available" in record.getMessage()
                for record in caplog.records
            )
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.last_delete_error is not None
                assert "No delete route available" in candidate.last_delete_error
                assert "Radarr" in candidate.last_delete_error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_series_route_is_logged_and_recorded(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_series_candidate(
                    db, removed_at=datetime.now(UTC)
                )

            _patch_services(monkeypatch)

            deleted, failed = await cleanup.delete_specific_candidates(
                [candidate_id], approved_by="tester"
            )

            assert (deleted, failed) == (0, 1)
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.last_delete_error is not None
                assert "No delete route available" in candidate.last_delete_error
                assert "Sonarr" in candidate.last_delete_error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_tombstoning_a_movie_drops_its_leftover_candidates(monkeypatch) -> None:
    """A candidate left on a tombstoned movie could never be deleted again."""

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_movie_candidate(db)
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                movie_id = candidate.movie_id
                assert movie_id is not None

            async with session_maker() as db:
                # no MovieVersion rows exist, so the movie is empty and tombstoned
                movie = await cleanup._soft_remove_movie_if_empty(db, movie_id)
                assert movie is not None
                assert movie.removed_at is not None
                await db.commit()

            async with session_maker() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_tombstoning_a_series_drops_its_leftover_candidates(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_series_candidate(
                    db, removed_at=datetime.now(UTC)
                )
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                series_id = candidate.series_id
                assert series_id is not None

            async with session_maker() as db:
                series = await cleanup._soft_remove_series_if_empty(db, series_id)
                assert series is not None
                assert series.removed_at is not None
                await db.commit()

            async with session_maker() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_retry_replaces_the_previous_attempts_error(monkeypatch) -> None:
    """A stale error must not suppress the current attempt's diagnosis."""

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_movie_candidate(
                    db,
                    removed_at=datetime.now(UTC),
                    last_delete_error="Something that happened a week ago",
                )

            _patch_services(monkeypatch, radarr=FakeRadarr())

            deleted, failed = await cleanup.delete_specific_candidates(
                [candidate_id], approved_by="tester"
            )

            assert (deleted, failed) == (0, 1)
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.last_delete_error is not None
                assert "a week ago" not in candidate.last_delete_error
                assert "tombstoned in Reclaimerr" in candidate.last_delete_error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_series_media_server_failure_is_recorded(monkeypatch) -> None:
    """A media-server fallback error used to reach only the log, leaving the
    candidate with the generic 'failed before a scoped handler' text."""

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_live_series_candidate(
                    db, sonarr_series_id=None, plex_item_id="77"
                )

            _patch_services(
                monkeypatch, sonarr=FakeSonarr(), main_media_server=FailingMediaServer()
            )

            deleted, failed = await cleanup.delete_specific_candidates(
                [candidate_id], approved_by="tester"
            )

            assert (deleted, failed) == (0, 1)
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                error = candidate.last_delete_error or ""
                assert "could not delete 'Show1'" in error
                assert "library is locked" in error
                assert "scoped handler" not in error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_series_sonarr_and_fallback_failures_are_both_recorded(monkeypatch) -> None:
    """An empty Sonarr exception left a bare 'config 1:' with nothing after it,
    and the media-server fallback that followed reported nothing at all."""

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_live_series_candidate(
                    db, sonarr_series_id=12, plex_item_id=None
                )

            _patch_services(
                monkeypatch,
                sonarr=TimingOutSonarr(),
                main_media_server=FailingMediaServer(),
            )

            deleted, failed = await cleanup.delete_specific_candidates(
                [candidate_id], approved_by="tester"
            )

            assert (deleted, failed) == (0, 1)
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                error = candidate.last_delete_error or ""
                assert error.startswith(
                    "Sonarr unmonitor failed for config 1: TimeoutError | then: "
                )
                assert "not linked to an item on the main media server" in error
                # both reasons belong to one attempt
                assert candidate.delete_attempts == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unreachable_series_folder_names_the_path(monkeypatch, tmp_path) -> None:
    """Sonarr's folder not being visible to Reclaimerr (no mount or path
    mapping) used to surface only as 'Series file removal could not be
    confirmed'."""

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        missing = tmp_path / "tv" / "Show1"
        try:
            async with session_maker() as db:
                candidate_id = await _seed_live_series_candidate(
                    db,
                    sonarr_series_id=12,
                    plex_item_id=None,
                    sonarr_path=str(missing),
                )

            _patch_services(
                monkeypatch, sonarr=FakeSonarr(), main_media_server=FailingMediaServer()
            )

            deleted, failed = await cleanup.delete_specific_candidates(
                [candidate_id], approved_by="tester"
            )

            assert (deleted, failed) == (0, 1)
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                error = candidate.last_delete_error or ""
                assert f"Sonarr folder '{missing}' was not found" in error
                assert "no main media server item is linked" in error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_movie_media_server_failure_is_recorded(monkeypatch, tmp_path) -> None:
    """Same gap as series: the media-server error reached only the log."""

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id = await _seed_live_movie_candidate(
                    db,
                    radarr_movie_id=None,
                    plex_path=str(tmp_path / "Movie1" / "Movie1.mkv"),
                )

            _patch_services(
                monkeypatch, radarr=FakeRadarr(), main_media_server=FailingMediaServer()
            )

            deleted, failed = await cleanup.delete_specific_candidates(
                [candidate_id], approved_by="tester"
            )

            assert (deleted, failed) == (0, 1)
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                error = candidate.last_delete_error or ""
                assert "could not delete 'Movie1'" in error
                assert "library is locked" in error
                assert "scoped handler" not in error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unreachable_movie_file_names_the_path(monkeypatch, tmp_path) -> None:
    """A movie file Reclaimerr cannot see used to surface only as 'Movie file
    removal could not be confirmed'."""

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        missing = tmp_path / "Movie1" / "Movie1.mkv"
        try:
            async with session_maker() as db:
                candidate_id = await _seed_live_movie_candidate(
                    db, radarr_movie_id=55, plex_path=str(missing)
                )

            _patch_services(monkeypatch, radarr=FakeRadarr())

            deleted, failed = await cleanup.delete_specific_candidates(
                [candidate_id], approved_by="tester"
            )

            assert (deleted, failed) == (0, 1)
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                error = candidate.last_delete_error or ""
                assert f"plex file '{missing}' was not found" in error
                assert "no main media server item is linked" in error
        finally:
            await engine.dispose()

    asyncio.run(run())
