from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Episode,
    EpisodeVersion,
    GeneralSettings,
    ProtectedMedia,
    ReclaimCandidate,
    ReclaimHistory,
    ReclaimRule,
    Season,
    Series,
    SeriesArrRef,
    SeriesServiceRef,
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.models.live_playback import (
    LiveSession,
    PlaybackDeferral,
    PlaybackDeferred,
    PlaybackSnapshot,
)
from backend.services.playback_guard import (
    PlaybackGuard,
    PlaybackOperation,
    PlaybackTarget,
    current_playback_operation,
    guard_native_removal,
)
from backend.tasks import cleanup


class FakeMediaServer:
    def __init__(self) -> None:
        self.deleted_items: list[str] = []
        self.scanned_paths: list[str] = []

    async def delete_item(self, item_id: str) -> None:
        self.deleted_items.append(item_id)

    async def scan_item_path(self, item_path: str) -> bool:
        self.scanned_paths.append(item_path)
        return True


class FakeSonarr:
    def __init__(self, episodes_by_series: dict[int, list[dict[str, Any]]]) -> None:
        self.episodes_by_series = episodes_by_series
        self.get_episode_calls: list[int] = []
        self.deleted_episode_files: list[int] = []
        self.unmonitored_episodes: list[int] = []
        self.season_monitoring_updates: list[tuple[int, int, bool]] = []
        self.deleted_seasons: list[tuple[int, int]] = []
        self.deleted_series: list[int] = []
        self.deleted_series_requests: list[tuple[int, bool, bool]] = []
        self.refreshed: list[list[int]] = []

    async def get_episodes(
        self, series_id: int, season_number: int | None = None
    ) -> list[dict[str, Any]]:
        self.get_episode_calls.append(series_id)
        episodes = [dict(ep) for ep in self.episodes_by_series.get(series_id, [])]
        if season_number is not None:
            episodes = [
                ep for ep in episodes if ep.get("seasonNumber") == season_number
            ]
        return episodes

    async def delete_episode_file(self, episode_file_id: int) -> None:
        self.deleted_episode_files.append(episode_file_id)

    async def unmonitor_episode(self, episode_id: int) -> None:
        self.unmonitored_episodes.append(episode_id)

    async def update_season_monitoring(
        self, series_id: int, season_number: int, monitored: bool
    ) -> None:
        self.season_monitoring_updates.append((series_id, season_number, monitored))

    async def delete_season_files(self, series_id: int, season_number: int) -> None:
        self.deleted_seasons.append((series_id, season_number))

    async def get_series(self, series_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            seasons=[SimpleNamespace(statistics={"episodeFileCount": 1})]
        )

    async def delete_series(
        self,
        series_id: int,
        delete_files: bool = False,
        add_import_exclusion: bool = False,
    ) -> None:
        self.deleted_series.append(series_id)
        self.deleted_series_requests.append(
            (series_id, delete_files, add_import_exclusion)
        )

    async def refresh_series(self, series_ids: list[int]) -> None:
        self.refreshed.append(series_ids)

    async def unmonitor_series(self, series_ids: list[int]) -> None:
        pass


class FailingSonarr(FakeSonarr):
    async def delete_series(
        self,
        series_id: int,
        delete_files: bool = False,
        add_import_exclusion: bool = False,
    ) -> None:
        raise RuntimeError("sonarr unavailable")


@pytest.mark.parametrize(
    "outcome", ["deferred", "failed", "disabled", "no-server", "deleted", "local"]
)
def test_series_unmonitor_requires_confirmed_removal(monkeypatch, tmp_path, outcome):
    async def run():
        folder = tmp_path / "Show"
        if outcome == "local":
            folder.mkdir()
            (folder / "episode.mkv").write_bytes(b"episode")
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, configs, ids = await _seed_series_case(
                    db,
                    target_scope="series",
                    arr_action="unmonitor",
                    arr_series_paths=[str(folder)],
                    series_service_path=str(folder),
                    media_server_fallback_enabled=outcome != "disabled",
                )
                series_id = (await db.get(ReclaimCandidate, candidate_id)).series_id
            media = FakeMediaServer()
            if outcome in {"deferred", "local"}:
                media.delete_item = AsyncMock(
                    side_effect=PlaybackDeferred(
                        PlaybackDeferral("playback_unavailable", "2026-10-17", (1,))
                    )
                )
            elif outcome == "failed":
                media.delete_item = AsyncMock(
                    side_effect=ValueError("native removal failed")
                )
            _patch_services(
                monkeypatch,
                {configs[0]: FakeSonarr({ids[0]: []})},
                None if outcome == "no-server" else media,
            )
            monkeypatch.setattr(cleanup, "_dispatch_reclaim_event", AsyncMock())
            if outcome == "deferred":
                with pytest.raises(PlaybackDeferred):
                    await cleanup._delete_series_candidates(frozenset([candidate_id]))
            else:
                count = await cleanup._delete_series_candidates(
                    frozenset([candidate_id])
                )
                assert count == int(outcome in {"deleted", "local"})
            success = outcome in {"deleted", "local"}
            async with sessions() as db:
                assert (
                    (await db.get(Series, series_id)).removed_at is not None
                ) is success
                assert (await db.get(ReclaimCandidate, candidate_id) is None) is success
                assert len((await db.scalars(select(ReclaimHistory))).all()) == int(
                    success
                )
            if outcome == "local":
                media.delete_item.assert_not_awaited()
                assert media.scanned_paths == [str(folder)]
                assert not folder.exists()
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _make_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(cleanup, "async_db", session_maker)
    return engine, session_maker


@pytest.mark.parametrize(
    "snapshot,deferral_code",
    [
        (PlaybackSnapshot(), None),
        (PlaybackSnapshot(available=False), "playback_unavailable"),
        (PlaybackSnapshot((LiveSession("episode-key"),)), "currently_playing"),
    ],
)
def test_season_unmonitor_finishes_after_local_removal(
    monkeypatch, tmp_path, snapshot, deferral_code
):
    """Issue #486: Plex can have no deletion paths for an already removed season."""

    async def run():
        folder = tmp_path / "Show" / "Season 01"
        folder.mkdir(parents=True)
        episode = folder / "Show - S01E01.mkv"
        episode.write_bytes(b"episode")
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, configs, ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_action="unmonitor",
                    arr_series_paths=[str(folder.parent)],
                    season_path=str(folder),
                    episode_path=str(episode),
                )
            sonarr = FakeSonarr({ids[0]: [{"seasonNumber": 1, "episodeFileId": 900}]})
            media = FakeMediaServer()

            async def native_delete(item_id):
                await guard_native_removal(media, Service.PLEX, item_id)

            media.delete_item = AsyncMock(side_effect=native_delete)
            _patch_services(monkeypatch, {configs[0]: sonarr}, media)
            monkeypatch.setattr(cleanup.service_manager, "_plex_clients", {99: media})
            monkeypatch.setattr(
                "backend.services.live_sessions.native_item_paths",
                AsyncMock(return_value=[]),
            )
            guard = PlaybackGuard(True, [], {99: Service.PLEX})
            monkeypatch.setattr(guard, "_snapshot", AsyncMock(return_value=snapshot))
            token = current_playback_operation.set(
                PlaybackOperation(
                    guard,
                    PlaybackTarget(
                        paths=[(str(folder), None, None, True)],
                        identities={(99, "episode-key", None)},
                    ),
                )
            )
            monkeypatch.setattr(cleanup, "_dispatch_reclaim_event", AsyncMock())
            try:
                if deferral_code:
                    with pytest.raises(PlaybackDeferred) as error:
                        await cleanup._delete_season_candidates(
                            frozenset([candidate_id])
                        )
                    assert error.value.detail.code == deferral_code
                    assert folder.is_dir()
                    assert episode.read_bytes() == b"episode"
                    media.delete_item.assert_not_awaited()
                    assert media.scanned_paths == []
                    async with sessions() as db:
                        assert await db.get(ReclaimCandidate, candidate_id) is not None
                        assert (await db.scalars(select(ReclaimHistory))).all() == []
                    return
                deleted = await cleanup._delete_season_candidates(
                    frozenset([candidate_id])
                )
            finally:
                current_playback_operation.reset(token)

            assert deleted == 1
            assert not folder.exists()
            assert sonarr.season_monitoring_updates == [(ids[0], 1, False)]
            media.delete_item.assert_not_awaited()
            assert media.scanned_paths == [str(folder)]
            async with sessions() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None
                history = (await db.scalars(select(ReclaimHistory))).all()
                assert len(history) == 1
                assert history[0].size == 100
                assert history[0].action == "unmonitored"
        finally:
            await engine.dispose()

    asyncio.run(run())


def _patch_services(
    monkeypatch,
    sonarr_clients: dict[int, FakeSonarr],
    media_server: FakeMediaServer | None = None,
) -> None:
    monkeypatch.setattr(cleanup.service_manager, "_sonarr", None)
    monkeypatch.setattr(cleanup.service_manager, "_sonarr_clients", sonarr_clients)
    monkeypatch.setattr(cleanup.service_manager, "_radarr", None)
    monkeypatch.setattr(cleanup.service_manager, "_radarr_clients", {})
    monkeypatch.setattr(cleanup.service_manager, "_main_media_server", media_server)
    monkeypatch.setattr(cleanup.service_manager, "_plex", media_server)
    monkeypatch.setattr(cleanup.service_manager, "_jellyfin", None)
    monkeypatch.setattr(cleanup.service_manager, "_emby", None)
    monkeypatch.setattr(cleanup.service_manager, "_seerr", None)


async def _seed_series_case(
    db: AsyncSession,
    *,
    target_scope: str,
    media_server_fallback_enabled: bool = True,
    arr_series_paths: list[str | None] | None = None,
    arr_series_ids: list[int] | None = None,
    season_path: str = "/data/Show/Season 01",
    episode_path: str = "/data/Show/Season 01/Show - S01E01.mkv",
    path_mappings: list[dict] | None = None,
    arr_action: str = "delete",
    move_destination_series: str | None = None,
    series_service_path: str | None = None,
) -> tuple[int, list[int], list[int]]:
    arr_series_paths = arr_series_paths or ["/data/Show"]
    arr_series_ids = arr_series_ids or [
        10 + index for index in range(1, len(arr_series_paths) + 1)
    ]

    db.add(
        GeneralSettings(
            media_server_fallback_enabled=media_server_fallback_enabled,
            path_mappings=path_mappings or [],
            move_destination_series=move_destination_series,
        )
    )
    service_configs: list[ServiceConfig] = []
    for index, _path in enumerate(arr_series_paths, start=1):
        service_config = ServiceConfig(
            service_type=Service.SONARR,
            base_url=f"http://sonarr-{index}",
            api_key="secret",
            name=f"Sonarr {index}",
            enabled=True,
        )
        service_configs.append(service_config)
        db.add(service_config)

    series = Series(title="Show", tmdb_id=2000, year=2020, size=100)
    rule = ReclaimRule(
        name="Rule",
        media_type=MediaType.SERIES,
        enabled=True,
        target_scope=target_scope,
        definition={
            "version": 1,
            "root": {"type": "group", "op": "and", "children": []},
        },
        action={"candidate": True, "arr_action": arr_action},
    )
    db.add_all([series, rule])
    await db.flush()

    if series_service_path is not None:
        db.add(
            SeriesServiceRef(
                series_id=series.id,
                service=Service.PLEX,
                service_id="series-key",
                library_id="library-key",
                library_name="TV",
                path=series_service_path,
            )
        )

    season = Season(
        series_id=series.id,
        season_number=1,
        size=100,
        episode_count=1,
        path=season_path,
        episode_paths=[episode_path],
        plex_season_rating_key="season-key",
    )
    db.add(season)
    await db.flush()

    episode = Episode(
        season_id=season.id,
        episode_number=1,
        size=100,
        path=episode_path,
        plex_rating_key="episode-key",
    )
    db.add(episode)
    await db.flush()

    for service_config, arr_series_id, arr_series_path in zip(
        service_configs, arr_series_ids, arr_series_paths, strict=True
    ):
        db.add(
            SeriesArrRef(
                series_id=series.id,
                service_config_id=service_config.id,
                arr_series_id=arr_series_id,
                arr_series_path=arr_series_path,
                tmdb_id=series.tmdb_id,
            )
        )

    candidate = ReclaimCandidate(
        media_type=MediaType.SERIES,
        matched_rule_ids=[rule.id],
        matched_criteria={},
        reason="cleanup",
        reason_data=[],
        series_id=series.id,
        season_id=season.id if target_scope in {"season", "episode"} else None,
        episode_id=episode.id if target_scope == "episode" else None,
        estimated_space_bytes=episode.size
        if target_scope == "episode"
        else season.size,
    )
    db.add(candidate)
    await db.flush()
    candidate_id = candidate.id
    config_ids = [config.id for config in service_configs]
    await db.commit()
    return candidate_id, config_ids, arr_series_ids


def test_whole_series_without_active_ref_records_failure_when_fallback_disabled(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, _arr_ids = await _seed_series_case(
                    db,
                    target_scope="series",
                    media_server_fallback_enabled=False,
                )

            _patch_services(monkeypatch, {}, None)

            deleted = await cleanup._delete_series_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 0
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.delete_attempts == 1
                assert candidate.last_delete_error is not None
                assert "not handled by any active Sonarr instance" in (
                    candidate.last_delete_error
                )
            assert config_ids
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_whole_series_multi_target_rule_routes_only_to_selected_sonarr(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="series",
                    media_server_fallback_enabled=False,
                    arr_series_paths=["/data/Show", "/data4k/Show"],
                    arr_series_ids=[11, 22],
                )
                rule = (await db.execute(select(ReclaimRule))).scalar_one()
                rule.action = {
                    **(rule.action or {}),
                    "sonarr_service_config_ids": [config_ids[1]],
                }
                await db.commit()

            first = FakeSonarr({})
            second = FakeSonarr({})
            _patch_services(
                monkeypatch,
                {config_ids[0]: first, config_ids[1]: second},
            )

            deleted = await cleanup._delete_series_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 1
            assert first.deleted_series_requests == []
            assert second.deleted_series_requests == [(arr_ids[1], True, True)]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_whole_series_move_removes_sonarr_entry_without_deleting_archive(
    monkeypatch, tmp_path
) -> None:
    async def run() -> None:
        local_root = tmp_path / "data"
        series_dir = local_root / "Show"
        season_dir = series_dir / "Season 01"
        season_dir.mkdir(parents=True)
        episode_file = season_dir / "Show - S01E01.mkv"
        subtitle_file = season_dir / "Show - S01E01.eng.srt"
        episode_file.write_bytes(b"episode")
        subtitle_file.write_bytes(b"subtitle")
        destination_root = tmp_path / "archive"

        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="series",
                    path_mappings=[
                        {
                            "source_prefix": "/data",
                            "local_prefix": str(local_root).replace("\\", "/"),
                        }
                    ],
                    move_destination_series=str(destination_root),
                    series_service_path="/data/Show",
                )

            sonarr = FakeSonarr({})
            _patch_services(monkeypatch, {config_ids[0]: sonarr})

            moved, failed = await cleanup._move_specific_candidates_impl(
                [candidate_id],
                approved_by="tester",
            )

            assert (moved, failed) == (1, 0)
            assert sonarr.deleted_series_requests == [(arr_ids[0], False, True)]
            assert sonarr.refreshed == []
            assert not series_dir.exists()
            destination_series = destination_root / "Show"
            assert (
                destination_series / "Season 01" / episode_file.name
            ).read_bytes() == b"episode"
            assert (
                destination_series / "Season 01" / subtitle_file.name
            ).read_bytes() == b"subtitle"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_whole_series_sonarr_batch_failure_records_candidate_error(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="series",
                    media_server_fallback_enabled=False,
                )

            _patch_services(
                monkeypatch,
                {config_ids[0]: FailingSonarr({arr_ids[0]: []})},
                None,
            )

            deleted = await cleanup._delete_series_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 0
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.delete_attempts == 1
                assert candidate.last_delete_error is not None
                assert "sonarr unavailable" in candidate.last_delete_error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_series_candidate_shape_records_diagnosed_failure(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                db.add(
                    GeneralSettings(
                        media_server_fallback_enabled=False,
                        path_mappings=[],
                    )
                )
                candidate = ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="manual",
                    reason_data=[],
                    estimated_space_bytes=100,
                )
                db.add(candidate)
                await db.flush()
                candidate_id = candidate.id
                await db.commit()

            _patch_services(monkeypatch, {1: FakeSonarr({})}, None)

            deleted, failed = await cleanup.delete_specific_candidates(
                [candidate_id],
                approved_by="tester",
            )

            assert deleted == 0
            assert failed == 1
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.delete_attempts == 1
                assert candidate.last_delete_error is not None
                assert "not linked to a series" in candidate.last_delete_error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_episode_missing_in_sonarr_falls_back_to_media_server(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="episode",
                    media_server_fallback_enabled=True,
                )

            sonarr = FakeSonarr({arr_ids[0]: []})
            media = FakeMediaServer()
            _patch_services(monkeypatch, {config_ids[0]: sonarr}, media)

            deleted = await cleanup._delete_episode_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 1
            assert sonarr.deleted_episode_files == []
            assert media.deleted_items == ["episode-key"]
            async with session_maker() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None
                assert (await db.execute(select(Episode))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_episode_missing_in_sonarr_records_failure_when_fallback_disabled(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="episode",
                    media_server_fallback_enabled=False,
                )

            sonarr = FakeSonarr({arr_ids[0]: []})
            _patch_services(monkeypatch, {config_ids[0]: sonarr}, None)

            deleted = await cleanup._delete_episode_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 0
            async with session_maker() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.delete_attempts == 1
                assert (
                    candidate.last_delete_error == "No Sonarr episode found for S01E01"
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_episode_multi_sonarr_uses_path_matched_ref(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="episode",
                    arr_series_paths=["/data1/Show", "/data2/Show"],
                    arr_series_ids=[11, 22],
                    season_path="/data2/Show/Season 01",
                    episode_path="/data2/Show/Season 01/Show - S01E01.mkv",
                )

            wrong_sonarr = FakeSonarr({arr_ids[0]: []})
            matched_sonarr = FakeSonarr(
                {
                    arr_ids[1]: [
                        {
                            "id": 700,
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "episodeFileId": 900,
                        }
                    ]
                }
            )
            media = FakeMediaServer()
            _patch_services(
                monkeypatch,
                {config_ids[0]: wrong_sonarr, config_ids[1]: matched_sonarr},
                media,
            )

            deleted = await cleanup._delete_episode_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 1
            assert wrong_sonarr.get_episode_calls == []
            assert matched_sonarr.get_episode_calls == [22]
            assert matched_sonarr.deleted_episode_files == [900]
            assert media.deleted_items == []
            assert media.scanned_paths == ["/data2/Show/Season 01/Show - S01E01.mkv"]
            async with session_maker() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_season_multi_sonarr_uses_path_matched_ref(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_series_paths=["/data1/Show", "/data2/Show"],
                    arr_series_ids=[11, 22],
                    season_path="/data2/Show/Season 01",
                    episode_path="/data2/Show/Season 01/Show - S01E01.mkv",
                )

            wrong_sonarr = FakeSonarr({arr_ids[0]: []})
            matched_sonarr = FakeSonarr(
                {
                    arr_ids[1]: [
                        {
                            "id": 700,
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "episodeFileId": 900,
                        }
                    ]
                }
            )
            media = FakeMediaServer()
            _patch_services(
                monkeypatch,
                {config_ids[0]: wrong_sonarr, config_ids[1]: matched_sonarr},
                media,
            )

            deleted = await cleanup._delete_season_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 1
            # the unmatched instance is read to see whether it still holds a
            # copy, but is never asked to change anything
            assert wrong_sonarr.get_episode_calls == [11]
            assert wrong_sonarr.deleted_seasons == []
            assert wrong_sonarr.season_monitoring_updates == []
            assert wrong_sonarr.deleted_series == []
            assert matched_sonarr.get_episode_calls == [22]
            assert matched_sonarr.season_monitoring_updates == [(22, 1, False)]
            assert matched_sonarr.deleted_seasons == [(22, 1)]
            assert media.deleted_items == []
            assert media.scanned_paths == ["/data2/Show/Season 01"]
            async with session_maker() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None
                assert (await db.execute(select(Season))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_season_survives_when_another_sonarr_still_holds_a_copy(monkeypatch) -> None:
    """A show held twice keeps its season record when only one copy is deleted.

    HD and UHD copies share a single Season row, because seasons are keyed by
    series and number with nothing to tell the copies apart. Dropping the row
    after deleting one copy threw away the survivor's data and its protections,
    and the next sync rebuilt it as a new row with a fresh added date - which
    restarted the review period and flagged the surviving copy all over again.
    """

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_series_paths=["/data1/Show", "/data2/Show"],
                    arr_series_ids=[11, 22],
                    season_path="/data2/Show/Season 01",
                    episode_path="/data2/Show/Season 01/Show - S01E01.mkv",
                )

            # the HD copy still has its files on disk
            surviving_sonarr = FakeSonarr(
                {
                    arr_ids[0]: [
                        {
                            "id": 500,
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "episodeFileId": 800,
                            "hasFile": True,
                        }
                    ]
                }
            )
            matched_sonarr = FakeSonarr(
                {
                    arr_ids[1]: [
                        {
                            "id": 700,
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "episodeFileId": 900,
                        }
                    ]
                }
            )
            media = FakeMediaServer()
            _patch_services(
                monkeypatch,
                {config_ids[0]: surviving_sonarr, config_ids[1]: matched_sonarr},
                media,
            )

            deleted = await cleanup._delete_season_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 1
            # only the targeted copy's files go
            assert matched_sonarr.deleted_seasons == [(22, 1)]
            assert surviving_sonarr.deleted_seasons == []
            # the shared media-server item is not removed, because the stored id
            # may belong to the copy that is staying
            assert media.deleted_items == []
            assert media.scanned_paths == ["/data2/Show/Season 01"]
            async with session_maker() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None
                seasons = (await db.execute(select(Season))).scalars().all()
                assert len(seasons) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_season_delete_scans_media_server_path_when_fallback_disabled(
    monkeypatch,
) -> None:
    """The media server has to learn the files are gone even with fallback off.

    `delete_item` removes media, so it stays behind the fallback setting - but
    skipping the whole reconciliation leaves the server serving an entry whose
    files Sonarr already deleted. The next sync re-imports it and the scan
    re-flags it with a brand new review period, so it never actually goes away.
    """

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    media_server_fallback_enabled=False,
                )

            sonarr = FakeSonarr(
                {
                    arr_ids[0]: [
                        {
                            "id": 700,
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "episodeFileId": 900,
                        }
                    ]
                }
            )
            media = FakeMediaServer()
            _patch_services(monkeypatch, {config_ids[0]: sonarr}, media)

            deleted = await cleanup._delete_season_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 1
            assert sonarr.deleted_seasons == [(arr_ids[0], 1)]
            assert media.deleted_items == []
            assert media.scanned_paths == ["/data/Show/Season 01"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_episode_delete_scans_media_server_path_when_fallback_disabled(
    monkeypatch,
) -> None:
    """Episodes used to call `delete_item` regardless of the fallback setting.

    They now honor it like seasons and series do, and fall back to the
    non-destructive path scan so the library still stays in step.
    """

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="episode",
                    media_server_fallback_enabled=False,
                )

            sonarr = FakeSonarr(
                {
                    arr_ids[0]: [
                        {
                            "id": 700,
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "episodeFileId": 900,
                        }
                    ]
                }
            )
            media = FakeMediaServer()
            _patch_services(monkeypatch, {config_ids[0]: sonarr}, media)

            deleted = await cleanup._delete_episode_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 1
            assert sonarr.deleted_episode_files == [900]
            assert media.deleted_items == []
            assert media.scanned_paths == ["/data/Show/Season 01/Show - S01E01.mkv"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_whole_series_delete_scans_media_server_paths(monkeypatch) -> None:
    """Sonarr removed the files, so the media server only needs a path scan."""

    async def run() -> None:
        engine, session_maker = await _make_session(monkeypatch)
        try:
            async with session_maker() as db:
                candidate_id, config_ids, arr_ids = await _seed_series_case(
                    db,
                    target_scope="series",
                    media_server_fallback_enabled=False,
                    series_service_path="/data/Show",
                )

            sonarr = FakeSonarr({arr_ids[0]: []})
            media = FakeMediaServer()
            _patch_services(monkeypatch, {config_ids[0]: sonarr}, media)

            deleted = await cleanup._delete_series_candidates(
                restrict_to_ids=frozenset([candidate_id]),
                approved_by="tester",
            )

            assert deleted == 1
            assert sonarr.deleted_series == [arr_ids[0]]
            assert media.deleted_items == []
            assert media.scanned_paths == ["/data/Show"]
        finally:
            await engine.dispose()

    asyncio.run(run())


class MonitoringSonarr(FakeSonarr):
    def __init__(self, series_id=11, *, failure=None, latest=1):
        super().__init__(
            {
                series_id: [
                    {
                        "id": 700,
                        "seasonNumber": 1,
                        "episodeNumber": 1,
                        "episodeFileId": 900,
                        "hasFile": True,
                    }
                ]
            }
        )
        self.failure = failure
        self.latest = latest
        self.events = []

    async def prepare_season_removal(self, series_id, season_number):
        self.events.append("prepare")
        if self.failure == "prepare":
            raise RuntimeError("preparation unavailable")
        await self.update_season_monitoring(series_id, season_number, False)

    async def delete_season_files(self, series_id, season_number):
        self.events.append("delete")
        if self.failure == "delete":
            raise RuntimeError("file removal failed")
        await super().delete_season_files(series_id, season_number)
        self.episodes_by_series[series_id] = []

    async def enable_new_seasons_if_latest(self, series_id, season_number):
        self.events.append("finalize")
        if self.failure == "finalize":
            raise RuntimeError("monitoring update unavailable")
        return season_number == self.latest


@pytest.mark.parametrize(
    "failure,fallback,latest,expected,status",
    [
        (None, True, 1, 1, "enabled"),
        (None, True, 2, 1, "skipped"),
        ("prepare", True, 1, 0, None),
        ("delete", False, 1, 0, None),
        ("delete", True, 1, 1, "enabled"),
        ("finalize", True, 1, 1, "failed"),
    ],
)
def test_conditional_monitoring_season_delete(
    monkeypatch, failure, fallback, latest, expected, status
):
    async def run():
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, configs, ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_action=cleanup.ARR_ACTION_MONITOR_NEW_SEASONS,
                    media_server_fallback_enabled=fallback,
                )
            sonarr = MonitoringSonarr(ids[0], failure=failure, latest=latest)
            media = FakeMediaServer()
            _patch_services(monkeypatch, {configs[0]: sonarr}, media)
            deleted = await cleanup._delete_season_candidates(
                restrict_to_ids=frozenset([candidate_id]), approved_by="tester"
            )
            assert deleted == expected
            assert sonarr.deleted_series == []
            if expected:
                assert sonarr.events == ["prepare", "delete", "finalize"]
                # Native fallback is needed only when Sonarr did not remove files.
                assert media.deleted_items == (
                    ["season-key"] if failure == "delete" else []
                )
                assert media.scanned_paths == (
                    [] if failure == "delete" else ["/data/Show/Season 01"]
                )
            else:
                assert "finalize" not in sonarr.events
                assert media.deleted_items == []
            async with sessions() as db:
                history = (await db.execute(select(ReclaimHistory))).scalars().all()
                candidate = await db.get(ReclaimCandidate, candidate_id)
                if expected:
                    assert candidate is None
                    assert len(history) == 1
                    assert history[0].action == "unmonitored"
                    assert history[0].attributes["sonarr_monitor_new_seasons"] == status
                    if status == "failed":
                        assert (
                            "monitoring update unavailable"
                            in history[0].attributes["sonarr_monitor_new_seasons_error"]
                        )
                else:
                    assert candidate.last_delete_error
                    assert history == []
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "delete"])
@pytest.mark.parametrize("other_available", [True, False])
def test_conditional_monitoring_preserves_other_sonarr_copy(
    monkeypatch, failure, other_available
):
    async def run():
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, configs, ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_action=cleanup.ARR_ACTION_MONITOR_NEW_SEASONS,
                    arr_series_paths=["/data1/Show", "/data2/Show"],
                    arr_series_ids=[11, 22],
                    season_path="/data2/Show/Season 01",
                    episode_path="/data2/Show/Season 01/Show - S01E01.mkv",
                )
            other = MonitoringSonarr(ids[0])
            selected = MonitoringSonarr(ids[1], failure=failure)
            media = FakeMediaServer()
            _patch_services(
                monkeypatch,
                {configs[0]: other, configs[1]: selected}
                if other_available
                else {configs[1]: selected},
                media,
            )
            count = await cleanup._delete_season_candidates(
                restrict_to_ids=frozenset([candidate_id]), approved_by="tester"
            )
            assert count == (0 if failure else 1)
            assert other.events == []
            assert media.deleted_items == []
            assert selected.events == (
                ["prepare", "delete"] if failure else ["prepare", "delete", "finalize"]
            )
            async with sessions() as db:
                assert len((await db.execute(select(Season))).scalars().all()) == 1
                assert (await db.execute(select(Series))).scalar_one().size == 100
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure,latest,other_copy",
    [
        (None, 1, False),
        (None, 2, False),
        (None, 1, True),
        ("prepare", 1, False),
        ("finalize", 1, False),
        ("move", 1, False),
        ("route", 1, False),
        ("partial", 1, False),
        ("flat", 1, False),
        ("unavailable", 1, False),
    ],
)
def test_conditional_monitoring_season_move(
    monkeypatch, tmp_path, failure, latest, other_copy
):
    async def run():
        series_dir = tmp_path / "Show"
        season_dir = (
            series_dir if failure in {"partial", "flat"} else series_dir / "Season 01"
        )
        season_dir.mkdir(parents=True)
        episode_file = season_dir / "Show - S01E01.mkv"
        episode_file.write_bytes(b"episode")
        archive = tmp_path / "archive"
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, configs, ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_action=cleanup.ARR_ACTION_MONITOR_NEW_SEASONS,
                    arr_series_paths=[str(series_dir)]
                    + ([str(tmp_path / "OtherShow")] if other_copy else []),
                    arr_series_ids=[11, 22] if other_copy else [11],
                    season_path=str(season_dir),
                    episode_path=str(episode_file),
                    series_service_path=str(series_dir),
                    move_destination_series=str(archive),
                )
            async with sessions() as db:
                if failure == "route":
                    ref = (await db.execute(select(SeriesArrRef))).scalar_one()
                    ref.arr_series_path = "/unmatched/Show"
                    await db.commit()
            sonarr = MonitoringSonarr(ids[0], failure=failure, latest=latest)
            media = FakeMediaServer()
            clients = {configs[0]: sonarr}
            other = MonitoringSonarr(22)
            if other_copy:
                clients[configs[1]] = other
            _patch_services(
                monkeypatch, {} if failure == "unavailable" else clients, media
            )
            if failure == "partial":
                monkeypatch.setattr(
                    cleanup, "move_season_files", lambda *args, **kwargs: archive
                )
            if failure == "move":

                def fail_move(*args, **kwargs):
                    raise OSError("archive unavailable")

                monkeypatch.setattr(cleanup, "move_directory", fail_move)
            result = await cleanup._move_specific_candidates_impl(
                [candidate_id], approved_by="tester"
            )
            success = failure not in {
                "prepare",
                "move",
                "route",
                "unavailable",
                "partial",
            }
            assert result == ((1, 0) if success else (0, 1))
            assert sonarr.events == (
                ["prepare", "finalize"]
                if success
                else []
                if failure in {"route", "unavailable"}
                else ["prepare"]
            )
            assert other.events == []
            if other_copy:
                assert media.deleted_items == []
            assert sonarr.deleted_series == [] and sonarr.deleted_seasons == []
            assert episode_file.exists() is not success
            if success:
                assert list(archive.rglob("*.mkv"))[0].read_bytes() == b"episode"
            async with sessions() as db:
                history = (await db.execute(select(ReclaimHistory))).scalars().all()
                assert len(history) == int(success)
                if success:
                    assert history[0].action == "moved"
                    assert history[0].attributes["sonarr_monitor_new_seasons"] == (
                        "failed"
                        if failure == "finalize"
                        else "enabled"
                        if latest == 1
                        else "skipped"
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_season_move_kept_copy_scans_media_server_path(monkeypatch, tmp_path):
    """The re-scan sends the path the media server reported, not the local one
    path mappings resolved it to - the server does not know the local path."""

    async def run():
        local_root = tmp_path / "data"
        season_dir = local_root / "Show" / "Season 01"
        season_dir.mkdir(parents=True)
        (season_dir / "Show - S01E01.mkv").write_bytes(b"episode")
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, configs, ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_action=cleanup.ARR_ACTION_MONITOR_NEW_SEASONS,
                    arr_series_paths=["/data/Show", "/data/OtherShow"],
                    arr_series_ids=[11, 22],
                    path_mappings=[
                        {
                            "source_prefix": "/data",
                            "local_prefix": str(local_root).replace("\\", "/"),
                        }
                    ],
                    series_service_path="/data/Show",
                    move_destination_series=str(tmp_path / "archive"),
                )
            media = FakeMediaServer()
            _patch_services(
                monkeypatch,
                {
                    configs[0]: MonitoringSonarr(ids[0]),
                    configs[1]: MonitoringSonarr(22),
                },
                media,
            )
            result = await cleanup._move_specific_candidates_impl(
                [candidate_id], approved_by="tester"
            )
            assert result == (1, 0)
            assert media.deleted_items == []
            assert media.scanned_paths == ["/data/Show/Season 01"]
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("route", ["unavailable", "ambiguous", "selected", "no_files"])
def test_conditional_monitoring_requires_usable_sonarr_route(monkeypatch, route):
    async def run():
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, configs, ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_action=cleanup.ARR_ACTION_MONITOR_NEW_SEASONS,
                    arr_series_paths=["/sonarr1/Show", "/sonarr2/Show"],
                    arr_series_ids=[11, 22],
                )
                if route != "ambiguous":
                    rule = (await db.execute(select(ReclaimRule))).scalar_one()
                    rule.action = {
                        **rule.action,
                        "sonarr_service_config_ids": [configs[1]],
                    }
                    await db.commit()
            first = MonitoringSonarr(ids[0])
            second = MonitoringSonarr(ids[1])
            if route == "no_files":
                second.episodes_by_series[ids[1]][0]["episodeFileId"] = 0
            clients = (
                {configs[0]: first, configs[1]: second}
                if route != "unavailable"
                else {}
            )
            media = FakeMediaServer()
            _patch_services(monkeypatch, clients, media)
            count = await cleanup._delete_season_candidates(
                restrict_to_ids=frozenset([candidate_id]), approved_by="tester"
            )
            assert count == int(route == "selected")
            assert first.events == []
            assert second.events == (
                ["prepare", "delete", "finalize"] if route == "selected" else []
            )
            assert media.deleted_items == []
            async with sessions() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                if route != "selected":
                    assert candidate.last_delete_error
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "action", [cleanup.ARR_ACTION_LIST_EXCLUSION, cleanup.ARR_ACTION_MONITOR_OR_EXCLUDE]
)
@pytest.mark.parametrize("failure", [None, "prepare", "delete", "exclude"])
def test_season_exclusion_runs_only_after_successful_removal(
    monkeypatch, action, failure
):
    async def run():
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, configs, ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_action=action,
                    media_server_fallback_enabled=False,
                )
            sonarr = MonitoringSonarr(ids[0], failure=failure)

            async def exclude(series_id, season_number):
                assert sonarr.events == ["prepare", "delete"]
                sonarr.events.append("exclude")
                if failure == "exclude":
                    raise RuntimeError("exclusion unavailable")
                return True

            sonarr.exclude_ended_series_if_latest = exclude
            _patch_services(monkeypatch, {configs[0]: sonarr}, FakeMediaServer())
            count = await cleanup._delete_season_candidates(
                restrict_to_ids=frozenset([candidate_id]), approved_by="tester"
            )
            success = failure not in {"prepare", "delete"}
            assert count == int(success)
            assert ("exclude" in sonarr.events) is success
            assert "finalize" not in sonarr.events
            assert sonarr.deleted_series == []
            async with sessions() as db:
                history = (await db.execute(select(ReclaimHistory))).scalars().all()
                assert len(history) == int(success)
                if success:
                    assert history[0].action == "unmonitored"
                    assert history[0].attributes["arr_list_exclusion"] == (
                        "failed" if failure == "exclude" else "added"
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "action", [cleanup.ARR_ACTION_LIST_EXCLUSION, cleanup.ARR_ACTION_MONITOR_OR_EXCLUDE]
)
def test_season_move_adds_exclusion_after_source_removed(monkeypatch, tmp_path, action):
    async def run():
        series_dir = tmp_path / "Show"
        season_dir = series_dir / "Season 01"
        season_dir.mkdir(parents=True)
        source = season_dir / "Show - S01E01.mkv"
        source.write_bytes(b"episode")
        archive = tmp_path / "archive"
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate, configs, ids = await _seed_series_case(
                    db,
                    target_scope="season",
                    arr_action=action,
                    arr_series_paths=[str(series_dir)],
                    arr_series_ids=[11],
                    season_path=str(season_dir),
                    episode_path=str(source),
                    series_service_path=str(series_dir),
                    move_destination_series=str(archive),
                )
            sonarr = MonitoringSonarr(ids[0])

            async def exclude(series_id, season_number):
                assert not source.exists()
                assert sonarr.events == ["prepare"]
                sonarr.events.append("exclude")
                return True

            sonarr.exclude_ended_series_if_latest = exclude
            _patch_services(monkeypatch, {configs[0]: sonarr}, FakeMediaServer())
            assert await cleanup._move_specific_candidates_impl(
                [candidate], approved_by="tester"
            ) == (1, 0)
            assert sonarr.events == ["prepare", "exclude"]
            assert sonarr.deleted_series == []
            assert list(archive.rglob("*.mkv"))[0].read_bytes() == b"episode"
            async with sessions() as db:
                history = (await db.execute(select(ReclaimHistory))).scalar_one()
                assert history.action == "moved"
                assert history.attributes["arr_list_exclusion"] == "added"
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _add_combined_peer(db, kind):
    first = (await db.scalars(select(Episode))).one()
    second = Episode(
        season_id=first.season_id,
        episode_number=2,
        size=100,
        path=first.path if kind == "path" else "/data/Show/Season 01/other.mkv",
    )
    if kind in ("emby", "jellyfin"):
        setattr(first, f"{kind}_episode_id", "combined")
        setattr(second, f"{kind}_episode_id", "combined")
    db.add(second)
    await db.flush()
    if kind == "version":
        for episode in (first, second):
            db.add(
                EpisodeVersion(
                    episode_id=episode.id,
                    service=Service.EMBY,
                    service_item_id=f"item-{episode.id}",
                    service_media_id="shared-file",
                    library_id="tv",
                    library_name="TV",
                    path="/physical/combined.mkv",
                    size=100,
                )
            )
    season = await db.get(Season, first.season_id)
    season.episode_count = 2
    await db.commit()
    return first.id, second.id


@pytest.mark.parametrize("kind", ["path", "emby", "jellyfin", "version", "sonarr"])
@pytest.mark.parametrize("action", ["delete", "unmonitor_only"])
def test_combined_episode_delete_preserves_shared_file(monkeypatch, kind, action):
    async def run():
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, configs, arr_ids = await _seed_series_case(
                    db,
                    target_scope="episode",
                    arr_action=action,
                )
                if kind != "sonarr":
                    await _add_combined_peer(db, kind)
            sonarr = FakeSonarr(
                {
                    arr_ids[0]: [
                        {
                            "id": 11,
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "episodeFileId": 90,
                        },
                        {
                            "id": 12,
                            "seasonNumber": 1,
                            "episodeNumber": 2,
                            "episodeFileId": 90,
                        },
                    ]
                }
            )
            media = FakeMediaServer()
            _patch_services(monkeypatch, {configs[0]: sonarr}, media)
            count = await cleanup._delete_episode_candidates(frozenset([candidate_id]))
            assert count == (1 if action == "unmonitor_only" else 0)
            assert sonarr.deleted_episode_files == []
            assert media.deleted_items == []
            assert sonarr.unmonitored_episodes == (
                [11] if action == "unmonitor_only" else []
            )
            async with sessions() as db:
                assert len((await db.scalars(select(Episode))).all()) == (
                    1 if kind == "sonarr" else 2
                )
                assert (await db.scalars(select(Season))).one().size == 100
                assert (await db.scalars(select(Series))).one().size == 100
                candidate = await db.get(ReclaimCandidate, candidate_id)
                if action == "delete":
                    assert (
                        candidate.last_delete_error == cleanup.SHARED_EPISODE_FILE_ERROR
                    )
                    assert (await db.scalars(select(ReclaimHistory))).all() == []
                else:
                    assert candidate is None
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["path", "emby", "jellyfin", "version"])
def test_combined_episode_protection_covers_file_peer(monkeypatch, kind):
    async def run():
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                await _seed_series_case(db, target_scope="episode")
                first, second = await _add_combined_peer(db, kind)
                db.add(
                    ProtectedMedia(
                        media_type=MediaType.SERIES, episode_id=second, permanent=True
                    )
                )
                await db.commit()
            monkeypatch.setattr(
                cleanup, "_evaluate_rule_for_episode", lambda *args: True
            )
            async with sessions() as db:
                rules = list((await db.scalars(select(ReclaimRule))).all())
                records = await cleanup._collect_episode_candidate_records(
                    db, rules, target_ids={first}
                )
                assert records == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_combined_episode_move_stops_before_moving_file(monkeypatch, tmp_path):
    async def run():
        engine, sessions = await _make_session(monkeypatch)
        try:
            async with sessions() as db:
                candidate_id, _, _ = await _seed_series_case(db, target_scope="episode")
                await _add_combined_peer(db, "path")
                candidate = await db.get(ReclaimCandidate, candidate_id)
            moved = []
            monkeypatch.setattr(cleanup, "move_media", lambda *a, **k: moved.append(a))
            result = await cleanup._move_series_candidates(
                [candidate],
                destination_series_str=str(tmp_path / "archive"),
                path_mappings=[],
                rules_by_id={},
                series_arr_actions={},
                default_arr_delete_behavior="unmonitor",
                add_arr_import_exclusions_on_delete=True,
                approved_by="tester",
            )
            assert result == (0, 1)
            assert moved == []
            async with sessions() as db:
                assert len((await db.scalars(select(Episode))).all()) == 2
                assert (
                    await db.get(ReclaimCandidate, candidate_id)
                ).last_delete_error == cleanup.SHARED_EPISODE_FILE_ERROR
        finally:
            await engine.dispose()

    asyncio.run(run())
