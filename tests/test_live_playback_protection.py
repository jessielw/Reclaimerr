from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    DeleteRequest,
    Episode,
    GeneralSettings,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    SeriesServiceRef,
    ServiceConfig,
    SupplementalMediaMatch,
    UpgradeLeftover,
    User,
)
from backend.enums import MediaType, ProtectionRequestStatus, Service, UserRole
from backend.models.live_playback import LiveSession, PlaybackDeferred, PlaybackSnapshot
from backend.services.live_sessions import emby_sessions, plex_sessions
from backend.services.playback_guard import (
    PlaybackGuard,
    PlaybackOperation,
    PlaybackTarget,
    candidate_playback_target,
    current_playback_operation,
    playback_checkpoint,
)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"MediaContainer": {}},
        {"MediaContainer": {"Metadata": [None]}},
        {"MediaContainer": {"size": 1, "Metadata": []}},
    ],
)
def test_invalid_plex_data_is_not_empty(payload):
    assert not plex_sessions(payload).available


@pytest.mark.parametrize(
    "payload", [None, {}, [None], [{"NowPlayingItem": {}}], [{"NowPlayingItem": "bad"}]]
)
def test_invalid_emby_data_is_not_empty(payload):
    assert not emby_sessions(payload).available


def test_empty_idle_and_paused_sessions():
    assert plex_sessions({"MediaContainer": {"size": 0}}) == PlaybackSnapshot()
    assert emby_sessions([{"Id": "idle", "NowPlayingItem": None}]) == PlaybackSnapshot()
    plex = plex_sessions(
        {
            "MediaContainer": {
                "size": 1,
                "Metadata": [
                    {
                        "ratingKey": "7",
                        "Player": {"state": "paused"},
                        "Media": [{"id": 9, "Part": [{"file": "/movies/movie.mkv"}]}],
                    }
                ],
            }
        }
    )
    assert plex.sessions == (LiveSession("7", "9", ("/movies/movie.mkv",)),)
    emby = emby_sessions(
        [
            {
                "NowPlayingItem": {"Id": "7", "Path": "/wrong.mkv"},
                "PlayState": {"IsPaused": True, "MediaSourceId": "9"},
            }
        ]
    )
    assert emby.sessions == (LiveSession("7", "9"),)


def test_only_selected_plex_version_is_active():
    snapshot = plex_sessions(
        {
            "MediaContainer": {
                "Metadata": [
                    {
                        "ratingKey": "7",
                        "Media": [
                            {"id": "a", "selected": True, "Part": [{"file": "/a.mkv"}]},
                            {"id": "b", "Part": [{"file": "/b.mkv"}]},
                        ],
                    }
                ]
            }
        }
    )
    assert snapshot.sessions == (LiveSession("7", "a", ("/a.mkv",)),)


def test_scoped_ids_shared_paths_parents_and_distinct_versions():
    guard = PlaybackGuard(True, [], {1: Service.PLEX, 2: Service.PLEX})
    target = PlaybackTarget(
        paths=[("/a.mkv", Service.PLEX, 1, False)],
        identities={(1, "7", "9")},
        relevant_configs={1},
    )
    assert guard._matches(target, 1, LiveSession("7", "9"))
    assert not guard._matches(target, 2, LiveSession("7", "9", ("/other.mkv",)))
    assert not guard._matches(target, 1, LiveSession("7", "10", ("/other.mkv",)))
    assert guard._matches(target, 2, LiveSession("unrelated-id", "10", ("/a.mkv",)))
    assert guard._matches(
        target, 1, LiveSession("7")
    )  # Unknown version protects possible files.
    parent = PlaybackTarget(parents={(1, "series")})
    assert guard._matches(
        parent, 1, LiveSession("episode", parent_ids=("season", "series"))
    )


def test_mapped_paths_and_separate_copies():
    guard = PlaybackGuard(
        True,
        [
            {
                "service_config_id": 1,
                "source_prefix": "/plex",
                "local_prefix": "/storage",
            },
            {
                "service_config_id": 2,
                "source_prefix": "/emby",
                "local_prefix": "/storage",
            },
            {
                "service_config_id": 3,
                "source_prefix": "/copy",
                "local_prefix": "/different",
            },
        ],
        {1: Service.PLEX, 2: Service.EMBY, 3: Service.EMBY},
    )
    target = PlaybackTarget(
        paths=[("/plex/a.mkv", Service.PLEX, 1, False)], identities={(3, "7", None)}
    )
    assert guard._matches(target, 2, LiveSession("any", paths=("/emby/a.mkv",)))
    assert not guard._matches(target, 3, LiveSession("7", paths=("/copy/a.mkv",)))
    # Partially mapped multi-file operations cannot prove the copies disjoint.
    assert guard._matches(
        target, 3, LiveSession("7", paths=("/copy/a.mkv", "/unknown/b.mkv"))
    )
    target.paths.append(("/unmapped/b.mkv", Service.PLEX, 1, False))
    assert guard._matches(target, 3, LiveSession("7", paths=("/copy/a.mkv",)))


def test_unavailable_server_is_scoped_and_cache_refreshes(monkeypatch):
    async def run():
        from backend.core.service_manager import service_manager

        clock = [0.0]
        monkeypatch.setattr(
            "backend.services.playback_guard.monotonic", lambda: clock[0]
        )
        read = AsyncMock(return_value=PlaybackSnapshot())
        monkeypatch.setattr(
            type(service_manager),
            "get_media_server",
            lambda self, service, cid: (
                SimpleNamespace(get_live_sessions=read) if cid == 1 else None
            ),
        )
        guard = PlaybackGuard(True, [], {1: Service.PLEX, 2: Service.EMBY})
        target = PlaybackTarget(identities={(1, "7", None)}, relevant_configs={1})
        await guard.check(target)
        await guard.check(target)
        assert read.await_count == 1
        read.return_value = PlaybackSnapshot((LiveSession("7"),))
        clock[0] = 31
        with pytest.raises(PlaybackDeferred, match="currently playing"):
            await guard.check(target)
        assert read.await_count == 2
        with pytest.raises(PlaybackDeferred, match="unavailable"):
            await guard.check(PlaybackTarget(relevant_configs={2}))
        guard.enabled = False
        await guard.check(target)
        assert read.await_count == 2

    asyncio.run(run())


def test_season_descendants_shared_episode_files_and_supplemental_ownership(
    monkeypatch,
):
    async def run():
        engine, sessions = await _database(monkeypatch)
        try:
            config, _, _ = await _seed(sessions)
            async with sessions() as db:
                linked = ServiceConfig(
                    service_type=Service.PLEX,
                    base_url="http://other",
                    api_key="x",
                    name="linked",
                    enabled=True,
                )
                series = Series(title="Series", tmdb_id=8)
                db.add_all([linked, series])
                await db.flush()
                season = Season(
                    series_id=series.id,
                    season_number=1,
                    path="/series/Season 1",
                    plex_season_rating_key="s1",
                )
                db.add(season)
                await db.flush()
                episodes = [
                    Episode(
                        season_id=season.id,
                        episode_number=i,
                        path="/series/Season 1/double.mkv",
                        plex_rating_key=f"e{i}",
                    )
                    for i in (1, 2)
                ]
                db.add_all(episodes)
                db.add(
                    SeriesServiceRef(
                        series_id=series.id,
                        service=Service.PLEX,
                        service_id="series",
                        library_id="1",
                        library_name="TV",
                        path="/series",
                    )
                )
                await db.flush()
                db.add(
                    SupplementalMediaMatch(
                        source_service=Service.PLEX,
                        source_service_config_id=linked.id,
                        source_item_id="linked-episode",
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                        season_id=season.id,
                        episode_id=episodes[0].id,
                    )
                )
                await db.commit()
                candidate = ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    episode_id=episodes[0].id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="episode",
                )
                target = await candidate_playback_target(db, candidate)
                guard = await PlaybackGuard.load(db)
                assert guard._matches(
                    target,
                    config.id,
                    LiveSession("e2", paths=("/series/Season 1/double.mkv",)),
                )
                assert guard._matches(target, linked.id, LiveSession("linked-episode"))
                assert not guard._matches(
                    target, config.id, LiveSession("linked-episode")
                )
                candidate.episode_id = None
                target = await candidate_playback_target(db, candidate)
                assert guard._matches(
                    target, config.id, LiveSession("new-episode", parent_ids=("s1",))
                )
                assert guard._matches(
                    target,
                    config.id,
                    LiveSession("new-episode", paths=("/series/Season 1/new.mkv",)),
                )
                assert not guard._matches(
                    target,
                    config.id,
                    LiveSession("other", paths=("/series/Season 2/other.mkv",)),
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_duplicate_and_leftover_deferral_preserve_files(monkeypatch, tmp_path):
    async def run():
        from backend.jobs import duplicate_file_ops
        from backend.models.jobs import DuplicateDeleteJobItem, LeftoverDeleteJobItem

        engine, sessions = await _database(monkeypatch)
        try:
            config, movie, candidates = await _seed(sessions)
            async with sessions() as db:
                for version in (await db.scalars(select(MovieVersion))).all():
                    version.size = 100
                await db.commit()
            guard = PlaybackGuard(True, [], {config.id: Service.PLEX})
            monkeypatch.setattr(
                guard,
                "_snapshot",
                AsyncMock(
                    return_value=PlaybackSnapshot((LiveSession("7", "1", ("/1.mkv",)),))
                ),
            )
            implementation = AsyncMock()
            monkeypatch.setattr(
                duplicate_file_ops, "_delete_duplicate_files_impl", implementation
            )
            with pytest.raises(PlaybackDeferred):
                await duplicate_file_ops.delete_duplicate_files(
                    DuplicateDeleteJobItem(
                        media_type=MediaType.MOVIE,
                        item_id=movie.id,
                        version_ids=[candidates[0].movie_version_id],
                        display_label="Movie",
                    ),
                    approved_by="test",
                    playback_guard=guard,
                )
            implementation.assert_not_awaited()
            # A retained version playing does not block deleting the other copy.
            implementation.return_value = 100
            assert (
                await duplicate_file_ops.delete_duplicate_files(
                    DuplicateDeleteJobItem(
                        media_type=MediaType.MOVIE,
                        item_id=movie.id,
                        version_ids=[candidates[1].movie_version_id],
                        display_label="Movie",
                    ),
                    approved_by="test",
                    playback_guard=guard,
                )
                == 100
            )
            local = tmp_path / "leftover.mkv"
            local.write_bytes(b"media")
            async with sessions() as db:
                leftover = UpgradeLeftover(
                    service_config_id=config.id,
                    arr_movie_id=1,
                    title="old",
                    size=5,
                    link_count=1,
                    dropped_path=str(local),
                    local_path=str(local),
                )
                db.add(leftover)
                await db.commit()
            monkeypatch.setattr(
                guard,
                "_snapshot",
                AsyncMock(
                    return_value=PlaybackSnapshot(
                        (LiveSession("old", paths=(str(local),)),)
                    )
                ),
            )
            with pytest.raises(PlaybackDeferred):
                await duplicate_file_ops.delete_leftover(
                    LeftoverDeleteJobItem(id=leftover.id, display_label="Old copy"),
                    approved_by="test",
                    playback_guard=guard,
                )
            assert local.read_bytes() == b"media"
            async with sessions() as db:
                assert await db.get(UpgradeLeftover, leftover.id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("provider", [Service.PLEX, Service.JELLYFIN, Service.EMBY])
def test_native_adapters_enrich_original_paths_and_reject_auth_failure(provider):
    async def run():
        from backend.services.emby_base import EmbyServiceBase
        from backend.services.plex import PlexService

        if provider is Service.PLEX:
            body = {
                "MediaContainer": {
                    "Metadata": [{"ratingKey": "7", "Media": [{"id": "9"}]}]
                }
            }
            metadata = (
                {
                    "MediaContainer": {
                        "Metadata": [
                            {
                                "Media": [
                                    {"id": "9", "Part": [{"file": "/original.mkv"}]}
                                ]
                            }
                        ]
                    }
                },
                200,
            )
            method = PlexService.get_live_sessions
        else:
            body = [
                {
                    "NowPlayingItem": {"Id": "7"},
                    "PlayState": {"IsPaused": True, "MediaSourceId": "9"},
                }
            ]
            metadata = {
                "Id": "7",
                "MediaSources": [{"Id": "9", "Path": "/original.mkv"}],
            }
            method = EmbyServiceBase.get_live_sessions
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
        client = SimpleNamespace(
            plex_url="http://test",
            service_url="http://test",
            service_type=provider,
            session=SimpleNamespace(get=AsyncMock(return_value=response)),
            _make_request=AsyncMock(return_value=metadata),
        )
        snapshot = await method(client)
        assert snapshot.available and snapshot.sessions[0].paths == ("/original.mkv",)

        def unauthorized():
            raise ValueError("unauthorized")

        response.raise_for_status = unauthorized
        with pytest.raises(ValueError, match="unauthorized"):
            await method(client)

    asyncio.run(run())


def test_playback_migration_enables_existing_settings_and_preserves_candidates():
    import importlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = importlib.import_module(
        "backend.alembic.versions.a6f3d8b2c901_add_live_playback_protection"
    )
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE general_settings (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE delete_requests (id INTEGER PRIMARY KEY)"))
        conn.execute(
            sa.text(
                "CREATE TABLE reclaim_candidates (id INTEGER PRIMARY KEY, reason TEXT)"
            )
        )
        conn.execute(sa.text("INSERT INTO general_settings VALUES (1)"))
        conn.execute(sa.text("INSERT INTO delete_requests VALUES (1)"))
        conn.execute(
            sa.text(
                "CREATE TABLE background_jobs (job_type TEXT, status TEXT, payload TEXT)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO background_jobs VALUES ('CANDIDATE_FILE_OP', 'PENDING', :payload)"
            ),
            {"payload": '{"delete_request_id": 1, "candidate_ids": [1]}'},
        )
        conn.execute(
            sa.text("INSERT INTO reclaim_candidates VALUES (1, 'keep deadline')")
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            assert (
                conn.execute(
                    sa.text(
                        "SELECT active_playback_protection_enabled FROM general_settings"
                    )
                ).scalar_one()
                == 1
            )
            assert conn.execute(
                sa.text(
                    "SELECT reason, playback_deferral, delete_request_id FROM reclaim_candidates"
                )
            ).one() == ("keep deadline", None, 1)
            migration.downgrade()
        assert (
            conn.execute(sa.text("SELECT reason FROM reclaim_candidates")).scalar_one()
            == "keep deadline"
        )
    engine.dispose()


def test_fresh_install_migrations_match_playback_models():
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        from pathlib import Path

        config = Config()
        config.set_main_option(
            "script_location",
            str(Path(__file__).resolve().parents[1] / "backend" / "alembic"),
        )
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
        inspector = sa.inspect(conn)
        assert "playback_deferral" in {
            c["name"] for c in inspector.get_columns("reclaim_candidates")
        }
        assert "delete_request_id" in {
            c["name"] for c in inspector.get_columns("reclaim_candidates")
        }
        assert "playback_deferral" in {
            c["name"] for c in inspector.get_columns("delete_requests")
        }
        assert "active_playback_protection_enabled" in {
            c["name"] for c in inspector.get_columns("general_settings")
        }
    engine.dispose()


def test_timeout_is_unavailable_and_cancellation_propagates(monkeypatch):
    async def run():
        from backend.core.service_manager import service_manager

        read = AsyncMock(side_effect=TimeoutError("secret URL"))
        monkeypatch.setattr(
            type(service_manager),
            "get_media_server",
            lambda *args: SimpleNamespace(get_live_sessions=read),
        )
        guard = PlaybackGuard(True, [], {1: Service.PLEX})
        with pytest.raises(PlaybackDeferred) as exc:
            await guard.check(PlaybackTarget(relevant_configs={1}))
        assert "secret" not in str(exc.value)
        guard.snapshots.clear()
        read.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await guard.check(PlaybackTarget(relevant_configs={1}))

    asyncio.run(run())


async def _database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("backend.tasks.cleanup.async_db", sessions)
    monkeypatch.setattr("backend.jobs.candidate_file_ops.async_db", sessions)
    monkeypatch.setattr("backend.jobs.duplicate_file_ops.async_db", sessions)
    return engine, sessions


async def _seed(sessions):
    async with sessions() as db:
        config = ServiceConfig(
            service_type=Service.PLEX,
            base_url="http://test",
            api_key="secret",
            enabled=True,
            is_main=True,
        )
        movie = Movie(title="Movie", tmdb_id=7)
        db.add_all([config, movie, GeneralSettings()])
        await db.flush()
        versions = [
            MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="7",
                service_media_id=str(i),
                library_id="1",
                library_name="Movies",
                path=f"/{i}.mkv",
            )
            for i in (1, 2)
        ]
        db.add_all(versions)
        await db.flush()
        candidates = [
            ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
                movie_version_id=v.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="test",
            )
            for v in versions
        ]
        db.add_all(candidates)
        await db.commit()
        return config, movie, candidates


@pytest.mark.parametrize("moving", [False, True])
def test_candidate_defers_before_prune_and_preserves_deadline(monkeypatch, moving):
    async def run():
        from backend.tasks import cleanup

        engine, sessions = await _database(monkeypatch)
        try:
            config, _, candidates = await _seed(sessions)
            guard = PlaybackGuard(True, [], {config.id: Service.PLEX})
            monkeypatch.setattr(
                guard,
                "_snapshot",
                AsyncMock(
                    return_value=PlaybackSnapshot((LiveSession("7", "1", ("/1.mkv",)),))
                ),
            )
            prune = AsyncMock()
            monkeypatch.setattr(
                cleanup, "_prune_leaving_soon_before_candidate_actions", prune
            )
            monkeypatch.setattr(
                cleanup, "_reconcile_leaving_soon_after_candidate_actions", AsyncMock()
            )
            implementation = AsyncMock(return_value=(1, 0))
            monkeypatch.setattr(
                cleanup,
                "_move_specific_candidates_impl"
                if moving
                else "_delete_specific_candidates_impl",
                implementation,
            )
            run = (
                cleanup.move_specific_candidates
                if moving
                else cleanup.delete_specific_candidates
            )
            result = await run([c.id for c in candidates], playback_guard=guard)
            assert (result.succeeded, result.failed, result.deferred) == (1, 0, 1)
            prune.assert_awaited_once_with([candidates[1].id])
            async with sessions() as db:
                retained = await db.get(ReclaimCandidate, candidates[0].id)
                assert retained.playback_deferral["code"] == "currently_playing"
                assert retained.created_at == candidates[0].created_at
                assert retained.delete_attempts == 0
                assert retained.last_delete_error is None
                assert retained.auto_delete_postponed_until is None
            monkeypatch.setattr(
                guard, "_snapshot", AsyncMock(return_value=PlaybackSnapshot())
            )
            retried = await run([candidates[0].id], playback_guard=guard)
            assert retried.deferred == 0
            async with sessions() as db:
                assert (
                    await db.get(ReclaimCandidate, candidates[0].id)
                ).playback_deferral is None
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "action",
    [
        {"arr_action": "unmonitor_only"},
        {"arr_action": "change_quality_profile", "quality_profile_id": 1},
    ],
)
def test_file_free_actions_do_not_fetch_playback(monkeypatch, action):
    async def run():
        from backend.tasks import cleanup

        engine, sessions = await _database(monkeypatch)
        try:
            config, _, candidates = await _seed(sessions)
            async with sessions() as db:
                rule = ReclaimRule(
                    name="rule", media_type=MediaType.MOVIE, action=action
                )
                db.add(rule)
                await db.flush()
                candidate = await db.get(ReclaimCandidate, candidates[0].id)
                candidate.matched_rule_ids = [rule.id]
                await db.commit()
            guard = PlaybackGuard(True, [], {config.id: Service.PLEX})
            read = AsyncMock(
                side_effect=AssertionError("No live-session read for file-free action")
            )
            monkeypatch.setattr(guard, "_snapshot", read)
            monkeypatch.setattr(
                cleanup, "_delete_candidates_prepared", AsyncMock(return_value=(1, 0))
            )
            result = await cleanup.delete_specific_candidates(
                [candidate.id], playback_guard=guard
            )
            assert result.succeeded == 1 and result.deferred == 0
            read.assert_not_awaited()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expanding_directory_scope_rechecks_playback(monkeypatch):
    async def run():
        guard = PlaybackGuard(True, [], {1: Service.PLEX})
        monkeypatch.setattr(
            guard,
            "_snapshot",
            AsyncMock(
                return_value=PlaybackSnapshot(
                    (LiveSession("other", paths=("/movie/other.mkv",)),)
                )
            ),
        )
        target = PlaybackTarget(
            paths=[("/movie/one.mkv", Service.PLEX, 1, False)], relevant_configs={1}
        )
        await guard.check(target)
        token = current_playback_operation.set(PlaybackOperation(guard, target))
        try:
            with pytest.raises(PlaybackDeferred):
                await playback_checkpoint(path="/movie", directory=True)
        finally:
            current_playback_operation.reset(token)

    asyncio.run(run())


def test_late_deferral_reports_completed_steps(monkeypatch):
    async def run():
        from backend.services.playback_guard import playback_step_completed

        guard = PlaybackGuard(True, [], {1: Service.PLEX})
        target = PlaybackTarget(identities={(1, "7", None)}, relevant_configs={1})
        monkeypatch.setattr(
            guard,
            "_snapshot",
            AsyncMock(return_value=PlaybackSnapshot((LiveSession("7"),))),
        )
        token = current_playback_operation.set(PlaybackOperation(guard, target))
        try:
            playback_step_completed("First file removed")
            with pytest.raises(PlaybackDeferred) as exc:
                await playback_checkpoint()
            assert exc.value.detail.completed_steps == ("First file removed",)
            assert "earlier step" in exc.value.detail.message
        finally:
            current_playback_operation.reset(token)

    asyncio.run(run())


def test_local_preflight_includes_folder_move_and_same_stem_cleanup(tmp_path):
    movie = tmp_path / "Movie"
    movie.mkdir()
    primary = movie / "Movie.mkv"
    primary.write_bytes(b"movie")
    extras = movie / "extras"
    extras.mkdir()
    trailer = extras / "trailer.mkv"
    trailer.write_bytes(b"trailer")
    guard = PlaybackGuard(True, [], {1: Service.PLEX})
    target = PlaybackTarget(paths=[(str(primary), None, None, False)])
    guard.expand_local_target(target, moving=True)
    assert guard._matches(target, 1, LiveSession("trailer", paths=(str(trailer),)))
    alternate = movie / "Movie.mp4"
    alternate.write_bytes(b"alternate")
    target = PlaybackTarget(paths=[(str(primary), None, None, False)])
    guard.expand_local_target(target, moving=False)
    assert guard._matches(target, 1, LiveSession("alternate", paths=(str(alternate),)))
    assert primary.read_bytes() == b"movie"
    assert alternate.read_bytes() == b"alternate"


def test_manual_job_reports_deferral_separately_and_keeps_approved_request(monkeypatch):
    async def run():
        from backend.enums import CandidateFileOpOperation
        from backend.jobs.candidate_file_ops import (
            queue_candidate_file_op_job,
            run_candidate_file_op_job,
        )
        from backend.models.jobs import CandidateFileOpJobPayload

        engine, sessions = await _database(monkeypatch)
        try:
            _, movie, candidates = await _seed(sessions)
            monkeypatch.setattr("backend.jobs.queue.async_db", sessions)
            monkeypatch.setattr(
                PlaybackGuard,
                "_snapshot",
                AsyncMock(
                    return_value=PlaybackSnapshot((LiveSession("7", "1", ("/1.mkv",)),))
                ),
            )
            notify = AsyncMock()
            monkeypatch.setattr("backend.jobs.candidate_file_ops.notify_user", notify)
            prune = AsyncMock()
            monkeypatch.setattr(
                "backend.tasks.cleanup._prune_leaving_soon_before_candidate_actions",
                prune,
            )
            async with sessions() as db:
                user = User(username="admin", password_hash="x", role=UserRole.ADMIN)
                db.add(user)
                await db.flush()
                request = DeleteRequest(
                    media_type=MediaType.MOVIE,
                    requested_by_user_id=user.id,
                    movie_id=movie.id,
                    status=ProtectionRequestStatus.APPROVED,
                )
                db.add(request)
                await db.commit()
            job = await queue_candidate_file_op_job(
                operation=CandidateFileOpOperation.DELETE,
                candidate_ids=[candidates[0].id],
                requested_by_user_id=user.id,
                requested_by_username=user.username,
                delete_request_id=request.id,
            )
            result = await run_candidate_file_op_job(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            assert (result["succeeded"], result["failed"], result["deferred"]) == (
                0,
                0,
                1,
            )
            assert result["errors"] == []
            assert "currently playing" in result["deferrals"][0]
            prune.assert_not_awaited()
            notify.assert_not_awaited()
            async with sessions() as db:
                retained = await db.get(ReclaimCandidate, candidates[0].id)
                assert retained.delete_request_id == request.id
                assert retained.delete_attempts == 0
                deferred = await db.get(DeleteRequest, request.id)
                assert deferred.playback_deferral["code"] == "currently_playing"
                assert deferred.executed_at is None
                assert deferred.status == ProtectionRequestStatus.APPROVED
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_deferred_request_survives_scan_and_can_retry_once(monkeypatch):
    async def run():
        from fastapi import HTTPException

        from backend.api.routes.delete_requests import retry_delete_request
        from backend.jobs.candidate_file_ops import _finalize_delete_request_job
        from backend.tasks.cleanup import _scan_with_db

        engine, sessions = await _database(monkeypatch)
        try:
            _, movie, candidates = await _seed(sessions)
            detail = {
                "code": "currently_playing",
                "message": "Deferred: currently playing",
                "checked_at": datetime.now(UTC).isoformat(),
                "server_config_ids": [1],
            }
            async with sessions() as db:
                user = User(username="admin", password_hash="x", role=UserRole.ADMIN)
                db.add(user)
                await db.flush()
                request = DeleteRequest(
                    media_type=MediaType.MOVIE,
                    requested_by_user_id=user.id,
                    movie_id=movie.id,
                    status=ProtectionRequestStatus.APPROVED,
                )
                db.add(request)
                await db.flush()
                candidate = await db.get(ReclaimCandidate, candidates[0].id)
                candidate.delete_request_id = request.id
                candidate.playback_deferral = detail
                await db.commit()
            await _finalize_delete_request_job(
                delete_request_id=request.id,
                candidate_ids=[candidate.id],
                succeeded=0,
                failed=0,
                deferred=1,
            )
            async with sessions() as db:
                retained = await db.get(DeleteRequest, request.id)
                assert retained.executed_at is None and retained.execution_error is None
                assert retained.playback_deferral == detail
                # No enabled rules: scan purges rule candidates, but preserves this request.
                await _scan_with_db(db)
                await db.commit()
                assert await db.get(ReclaimCandidate, candidate.id) is not None
            monkeypatch.setattr("backend.jobs.queue.async_db", sessions)
            async with sessions() as db:
                response = await retry_delete_request(request.id, user, db)
                assert response.playback_deferral is None
            async with sessions() as db:
                with pytest.raises(HTTPException) as exc:
                    await retry_delete_request(request.id, user, db)
                assert exc.value.status_code == 409
        finally:
            await engine.dispose()

    asyncio.run(run())
