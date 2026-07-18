from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Episode,
    GeneralSettings,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    SeriesArrRef,
    SeriesServiceRef,
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.tasks import cleanup


class FakeMediaServer:
    def __init__(self) -> None:
        self.deleted_items: list[str] = []

    async def delete_item(self, item_id: str) -> None:
        self.deleted_items.append(item_id)


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

    async def get_episodes(self, series_id: int) -> list[dict[str, Any]]:
        self.get_episode_calls.append(series_id)
        return [dict(ep) for ep in self.episodes_by_series.get(series_id, [])]

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


class FailingSonarr(FakeSonarr):
    async def delete_series(
        self,
        series_id: int,
        delete_files: bool = False,
        add_import_exclusion: bool = False,
    ) -> None:
        raise RuntimeError("sonarr unavailable")


async def _make_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(cleanup, "async_db", session_maker)
    return engine, session_maker


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
                assert "Sonarr delete failed" in candidate.last_delete_error
                assert "sonarr unavailable" in candidate.last_delete_error
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_series_candidate_shape_records_unexplained_failure(
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
                assert "failed before a scoped handler could complete" in (
                    candidate.last_delete_error
                )
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
            assert media.deleted_items == ["episode-key"]
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
            assert wrong_sonarr.get_episode_calls == []
            assert matched_sonarr.get_episode_calls == [22]
            assert matched_sonarr.season_monitoring_updates == [(22, 1, False)]
            assert matched_sonarr.deleted_seasons == [(22, 1)]
            assert media.deleted_items == ["season-key"]
            async with session_maker() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None
                assert (await db.execute(select(Season))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(run())
