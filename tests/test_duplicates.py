from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.duplicates import delete_duplicates, delete_leftovers
from backend.database import Base
from backend.database.models import (
    Episode,
    EpisodeVersion,
    GeneralSettings,
    Movie,
    MovieArrRef,
    MovieVersion,
    ProtectedMedia,
    ReclaimHistory,
    Season,
    Series,
    SeriesArrRef,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Permission, Service, UserRole
from backend.jobs import duplicate_file_ops
from backend.models.duplicates import (
    DuplicateDeleteItem,
    DuplicateDeleteRequest,
    LeftoverDeleteRequest,
)
from backend.models.jobs import DuplicateDeleteJobItem
from backend.models.media import AggregatedEpisodeData, EpisodeVersionData
from backend.services.duplicates import (
    MANUAL_INCOMPLETE,
    MANUAL_MULTI_EPISODE,
    MANUAL_SHARED_ITEM,
    DuplicateActionError,
    KeeperCriterion,
    fingerprint_files,
    load_duplicate_groups,
    normalize_keeper_priority,
    plan_duplicate_delete,
)
from backend.tasks.sync import _sync_episode_versions


async def _make_session() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )


def _movie_version(movie_id: int, media_id: str, **overrides: object) -> MovieVersion:
    fields: dict[str, object] = {
        "movie_id": movie_id,
        "service": Service.PLEX,
        "service_item_id": f"rk-{media_id}",
        "service_media_id": media_id,
        "library_id": "L1",
        "library_name": "Movies",
        "path": f"/data/movies/Movie1/{media_id}.mkv",
        "size": 1_000,
        "video_resolution": "1080",
        "video_width": 1920,
        "video_height": 1080,
        "video_codec_family": "h264",
    }
    fields.update(overrides)
    return MovieVersion(**fields)  # type: ignore[arg-type]


async def _seed_movie(db: AsyncSession, *versions: dict[str, Any]) -> int:
    movie = Movie(title="Movie One", tmdb_id=101, year=2020)
    db.add(movie)
    await db.flush()
    for index, overrides in enumerate(versions):
        db.add(
            _movie_version(
                movie.id, overrides.pop("media_id", f"m{index}"), **overrides
            )
        )
    await db.commit()
    return movie.id


async def _seed_episode(db: AsyncSession) -> tuple[int, int]:
    series = Series(title="Show", tmdb_id=202, year=2019)
    db.add(series)
    await db.flush()
    season = Season(series_id=series.id, season_number=1)
    db.add(season)
    await db.flush()
    ep1 = Episode(season_id=season.id, episode_number=1)
    ep2 = Episode(season_id=season.id, episode_number=2)
    db.add_all([ep1, ep2])
    await db.flush()
    return ep1.id, ep2.id


def _episode_version(
    episode_id: int, media_id: str, **overrides: object
) -> EpisodeVersion:
    fields: dict[str, object] = {
        "episode_id": episode_id,
        "service": Service.PLEX,
        "service_item_id": f"rk-{media_id}",
        "service_media_id": media_id,
        "library_id": "L2",
        "library_name": "TV",
        "path": f"/data/tv/Show/{media_id}.mkv",
        "size": 500,
        "video_height": 1080,
        "video_width": 1920,
    }
    fields.update(overrides)
    return EpisodeVersion(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def test_same_file_in_two_libraries_is_not_a_duplicate() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                await _seed_movie(
                    db,
                    {"media_id": "a", "path": "/data/movies/Movie1/Movie1.mkv"},
                    {
                        "media_id": "b",
                        "path": "/data/movies/Movie1/Movie1.mkv",
                        "library_id": "L9",
                        "library_name": "Kids",
                    },
                )
                assert await load_duplicate_groups(db) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_movie_group_suggests_the_better_file_as_keeper() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                await _seed_movie(
                    db,
                    {"media_id": "hd", "size": 4_000},
                    {
                        "media_id": "uhd",
                        "size": 20_000,
                        "video_resolution": "4k",
                        "video_width": 3840,
                        "video_height": 2160,
                        "video_hdr": True,
                    },
                )
                groups = await load_duplicate_groups(db)
                assert len(groups) == 1
                group = groups[0]
                assert group.manual_reason is None
                assert not group.cross_library
                assert [f.path for f in group.files] == [
                    "/data/movies/Movie1/uhd.mkv",
                    "/data/movies/Movie1/hd.mkv",
                ]
                assert group.reclaimable_size == 4_000
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_size_criterion_can_prefer_the_smaller_file() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                await _seed_movie(
                    db,
                    {"media_id": "big", "size": 9_000},
                    {"media_id": "small", "size": 3_000},
                )
                priority = normalize_keeper_priority(
                    [{"key": "size_smaller", "enabled": True}]
                )
                groups = await load_duplicate_groups(db, priority=priority)
                assert groups[0].files[0].path == "/data/movies/Movie1/small.mkv"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cross_library_group_is_flagged() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                await _seed_movie(
                    db,
                    {"media_id": "hd"},
                    {"media_id": "uhd", "library_id": "L4K", "library_name": "4K"},
                )
                groups = await load_duplicate_groups(db)
                assert groups[0].cross_library is True
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("second", "reason"),
    [
        ({"media_id": "b", "size": 0}, MANUAL_INCOMPLETE),
        ({"media_id": "b", "path": None}, MANUAL_INCOMPLETE),
        (
            {"media_id": "b", "service": Service.JELLYFIN, "service_item_id": "same"},
            MANUAL_SHARED_ITEM,
        ),
    ],
)
def test_manual_review_reasons(second: dict[str, Any], reason: str) -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                first: dict[str, Any] = {"media_id": "a"}
                if second.get("service") is Service.JELLYFIN:
                    first.update(service=Service.JELLYFIN, service_item_id="same")
                await _seed_movie(db, first, dict(second))
                groups = await load_duplicate_groups(db)
                assert groups[0].manual_reason == reason
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_episode_groups_and_multi_episode_files() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                ep1, ep2 = await _seed_episode(db)
                db.add_all(
                    [
                        _episode_version(ep1, "e1a"),
                        _episode_version(
                            ep1, "e1b", video_height=2160, video_width=3840
                        ),
                        # S01E02 has its own copy plus the S01E01-E02 double file
                        _episode_version(ep2, "e2a"),
                        _episode_version(
                            ep2, "multi", path="/data/tv/Show/S01E01-E02.mkv"
                        ),
                        _episode_version(
                            ep1, "multi1", path="/data/tv/Show/S01E01-E02.mkv"
                        ),
                    ]
                )
                await db.commit()
                groups = {g.item_id: g for g in await load_duplicate_groups(db)}
                assert set(groups) == {ep1, ep2}
                assert groups[ep1].manual_reason == MANUAL_MULTI_EPISODE
                assert groups[ep2].manual_reason == MANUAL_MULTI_EPISODE
                assert groups[ep1].episode_number == 1
                assert groups[ep1].season_number == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_fingerprint_changes_when_files_change() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                movie_id = await _seed_movie(db, {"media_id": "a"}, {"media_id": "b"})
                before = (await load_duplicate_groups(db))[0]
                db.add(_movie_version(movie_id, "c"))
                await db.commit()
                after = (await load_duplicate_groups(db))[0]
                assert fingerprint_files(before.files) != fingerprint_files(after.files)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_keeper_priority_normalization() -> None:
    priority = normalize_keeper_priority(
        [
            {"key": "bogus", "enabled": True},
            {"key": "size_larger", "enabled": True},
            {"key": "size_smaller", "enabled": True},
            {"key": "size_larger", "enabled": False},
        ]
    )
    as_dict = dict(priority)
    assert priority[0] == (KeeperCriterion.SIZE_LARGER, True)
    assert as_dict[KeeperCriterion.SIZE_SMALLER] is False
    assert len(priority) == len(KeeperCriterion)
    assert as_dict[KeeperCriterion.RESOLUTION] is False


# ---------------------------------------------------------------------------
# delete planning
# ---------------------------------------------------------------------------


def test_plan_refuses_unsafe_deletes() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                movie_id = await _seed_movie(db, {"media_id": "a"}, {"media_id": "b"})
                ids = [v.id for v in (await db.execute(select(MovieVersion))).scalars()]

                with pytest.raises(DuplicateActionError, match="kept"):
                    await plan_duplicate_delete(
                        db,
                        media_type=MediaType.MOVIE,
                        item_id=movie_id,
                        version_ids=ids,
                    )
                with pytest.raises(DuplicateActionError, match="refresh"):
                    await plan_duplicate_delete(
                        db,
                        media_type=MediaType.MOVIE,
                        item_id=movie_id,
                        version_ids=[9999],
                    )

                db.add(
                    ProtectedMedia(
                        media_type=MediaType.MOVIE,
                        movie_id=movie_id,
                        movie_version_id=ids[0],
                        permanent=True,
                    )
                )
                await db.commit()
                with pytest.raises(DuplicateActionError, match="protected"):
                    await plan_duplicate_delete(
                        db,
                        media_type=MediaType.MOVIE,
                        item_id=movie_id,
                        version_ids=[ids[0]],
                    )
                group, selected = await plan_duplicate_delete(
                    db,
                    media_type=MediaType.MOVIE,
                    item_id=movie_id,
                    version_ids=[ids[1]],
                )
                assert [f.version_ids for f in selected] == [[ids[1]]]
                assert len(group.files) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_plan_refuses_manual_groups() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                movie_id = await _seed_movie(
                    db, {"media_id": "a"}, {"media_id": "b", "size": 0}
                )
                ids = [v.id for v in (await db.execute(select(MovieVersion))).scalars()]
                with pytest.raises(DuplicateActionError, match="manual review"):
                    await plan_duplicate_delete(
                        db,
                        media_type=MediaType.MOVIE,
                        item_id=movie_id,
                        version_ids=[ids[1]],
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# delete job
# ---------------------------------------------------------------------------


class _FakeRadarr:
    def __init__(self, files: list[dict[str, Any]]) -> None:
        self.files = files
        self.deleted_file_ids: list[list[int]] = []
        self.unmonitored: list[list[int]] = []
        self.deleted_movies: list[list[int]] = []

    async def get_movie_files(self, movie_id: int) -> list[dict[str, Any]]:
        return [dict(f) for f in self.files]

    async def delete_movie_files(self, ids: list[int]) -> None:
        self.deleted_file_ids.append(ids)

    async def unmonitor_movies(self, ids: list[int]) -> None:
        self.unmonitored.append(ids)

    async def delete_movies(self, ids: list[int], **_: Any) -> None:
        self.deleted_movies.append(ids)

    async def rescan_movies(self, ids: list[int]) -> None:
        pass


class _FakeSonarr:
    def __init__(self, files: list[dict[str, Any]]) -> None:
        self.files = files
        self.deleted_file_ids: list[int] = []

    async def get_episode_files(self, series_id: int) -> list[dict[str, Any]]:
        return [dict(f) for f in self.files]

    async def delete_episode_file(self, file_id: int) -> None:
        self.deleted_file_ids.append(file_id)

    async def refresh_series(self, ids: list[int]) -> None:
        pass


class _FakeMediaServer:
    def __init__(self) -> None:
        self.deleted_versions: list[tuple[str, str]] = []
        self.scanned: list[str] = []

    async def delete_movie_version(self, item_id: str, media_id: str) -> None:
        self.deleted_versions.append((item_id, media_id))

    async def scan_item_path(self, path: str) -> bool:
        self.scanned.append(path)
        return True


def _patch(
    monkeypatch,
    sm,
    *,
    radarr: _FakeRadarr | None,
    media: _FakeMediaServer | None,
    sonarr: _FakeSonarr | None = None,
) -> list[dict[str, Any]]:
    from backend.core.service_manager import service_manager
    from backend.tasks import cleanup

    monkeypatch.setattr(duplicate_file_ops, "async_db", sm)
    monkeypatch.setattr(cleanup, "async_db", sm)
    monkeypatch.setattr(service_manager, "_radarr", None)
    monkeypatch.setattr(
        service_manager, "_radarr_clients", {1: radarr} if radarr else {}
    )
    monkeypatch.setattr(service_manager, "_sonarr", None)
    monkeypatch.setattr(
        service_manager, "_sonarr_clients", {1: sonarr} if sonarr else {}
    )
    monkeypatch.setattr(service_manager, "_main_media_server", media)
    monkeypatch.setattr(service_manager, "_plex", media)
    monkeypatch.setattr(service_manager, "_jellyfin", None)
    monkeypatch.setattr(service_manager, "_emby", None)
    events: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(duplicate_file_ops, "_dispatch_reclaim_event", _record)
    return events


async def _seed_radarr_case(
    db: AsyncSession,
    *,
    fallback: bool,
    uhd_path: str = "/data/movies/Movie1/Movie1-2160p.mkv",
    arr_movie_path: str | None = "/data/movies/Movie1",
) -> tuple[int, list[int]]:
    db.add(GeneralSettings(media_server_fallback_enabled=fallback))
    config = ServiceConfig(
        service_type=Service.RADARR, base_url="http://radarr", api_key="k", enabled=True
    )
    db.add(config)
    await db.flush()
    movie_id = await _seed_movie(
        db,
        {
            "media_id": "hd",
            "path": "/data/movies/Movie1/Movie1-1080p.mkv",
            "size": 4_000,
        },
        {
            "media_id": "uhd",
            "path": uhd_path,
            "size": 20_000,
            "video_height": 2160,
            "video_width": 3840,
        },
    )
    movie = await db.get(Movie, movie_id)
    assert movie is not None
    movie.size = 24_000
    db.add(
        MovieArrRef(
            movie_id=movie_id,
            service_config_id=config.id,
            arr_movie_id=55,
            arr_movie_path=arr_movie_path,
        )
    )
    await db.commit()
    rows = (
        (await db.execute(select(MovieVersion).order_by(MovieVersion.id)))
        .scalars()
        .all()
    )
    return movie_id, [r.id for r in rows]


def test_job_deletes_only_the_picked_file_via_radarr(monkeypatch) -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                movie_id, ids = await _seed_radarr_case(db, fallback=False)
            radarr = _FakeRadarr(
                [
                    {"id": 901, "path": "/data/movies/Movie1/Movie1-1080p.mkv"},
                    {"id": 902, "path": "/data/movies/Movie1/Movie1-2160p.mkv"},
                ]
            )
            media = _FakeMediaServer()
            events = _patch(monkeypatch, sm, radarr=radarr, media=media)

            freed = await duplicate_file_ops.delete_duplicate_files(
                DuplicateDeleteJobItem(
                    media_type=MediaType.MOVIE,
                    item_id=movie_id,
                    version_ids=[ids[0]],
                    display_label="Movie One",
                ),
                approved_by="tester",
            )

            assert freed == 4_000
            assert radarr.deleted_file_ids == [[901]]
            # the entry keeps its better file: never unmonitored or removed
            assert radarr.unmonitored == []
            assert radarr.deleted_movies == []
            assert media.deleted_versions == []
            assert len(events) == 1
            async with sm() as db:
                remaining = (await db.execute(select(MovieVersion))).scalars().all()
                assert [v.id for v in remaining] == [ids[1]]
                movie = await db.get(Movie, movie_id)
                assert movie is not None and movie.size == 20_000
                history = (await db.execute(select(ReclaimHistory))).scalars().all()
                assert [h.path for h in history] == [
                    "/data/movies/Movie1/Movie1-1080p.mkv"
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_job_falls_back_to_media_server_only_when_allowed(monkeypatch) -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                movie_id, ids = await _seed_radarr_case(db, fallback=False)
            radarr = _FakeRadarr([])  # Radarr tracks neither file
            media = _FakeMediaServer()
            _patch(monkeypatch, sm, radarr=radarr, media=media)
            monkeypatch.setattr(duplicate_file_ops, "sibling_cleanup", lambda _p: None)
            item = DuplicateDeleteJobItem(
                media_type=MediaType.MOVIE,
                item_id=movie_id,
                version_ids=[ids[0]],
                display_label="Movie One",
            )

            with pytest.raises(DuplicateActionError, match="fallback"):
                await duplicate_file_ops.delete_duplicate_files(item, approved_by="t")
            assert media.deleted_versions == []

            async with sm() as db:
                settings = (await db.execute(select(GeneralSettings))).scalars().one()
                settings.media_server_fallback_enabled = True
                await db.commit()
            await duplicate_file_ops.delete_duplicate_files(item, approved_by="t")
            assert media.deleted_versions == [("rk-hd", "hd")]
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("uhd_path", "arr_movie_path"),
    [
        # kept copy lives in a separate 4K folder Radarr doesn't manage
        ("/data/movies-4k/Movie1/Movie1-2160p.mkv", "/data/movies/Movie1"),
        # Radarr's folder isn't known yet, so it can't be checked
        ("/data/movies/Movie1/Movie1-2160p.mkv", None),
    ],
)
def test_job_refuses_radarr_delete_when_kept_copy_is_outside_its_folder(
    monkeypatch, uhd_path: str, arr_movie_path: str | None
) -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                movie_id, ids = await _seed_radarr_case(
                    db,
                    fallback=True,
                    uhd_path=uhd_path,
                    arr_movie_path=arr_movie_path,
                )
            radarr = _FakeRadarr(
                [{"id": 901, "path": "/data/movies/Movie1/Movie1-1080p.mkv"}]
            )
            media = _FakeMediaServer()
            _patch(monkeypatch, sm, radarr=radarr, media=media)

            with pytest.raises(DuplicateActionError, match="download it again"):
                await duplicate_file_ops.delete_duplicate_files(
                    DuplicateDeleteJobItem(
                        media_type=MediaType.MOVIE,
                        item_id=movie_id,
                        version_ids=[ids[0]],
                        display_label="Movie One",
                    ),
                    approved_by="t",
                )
            # nothing removed anywhere, not even through the media server
            assert radarr.deleted_file_ids == []
            assert media.deleted_versions == []
            async with sm() as db:
                remaining = (await db.execute(select(MovieVersion))).scalars().all()
                assert len(remaining) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_job_checks_every_radarr_instance_before_deleting(monkeypatch) -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                movie_id, ids = await _seed_radarr_case(db, fallback=True)
                # a second instance also tracks the file, but from another folder
                other = ServiceConfig(
                    service_type=Service.RADARR,
                    base_url="http://radarr-2",
                    name="radarr-2",
                    api_key="k",
                    enabled=True,
                )
                db.add(other)
                await db.flush()
                db.add(
                    MovieArrRef(
                        movie_id=movie_id,
                        service_config_id=other.id,
                        arr_movie_id=66,
                        arr_movie_path="/data/other/Movie1",
                    )
                )
                await db.commit()
                other_id = other.id
            tracked = [{"id": 901, "path": "/data/movies/Movie1/Movie1-1080p.mkv"}]
            first, second = _FakeRadarr(tracked), _FakeRadarr(tracked)
            _patch(monkeypatch, sm, radarr=first, media=_FakeMediaServer())
            from backend.core.service_manager import service_manager

            monkeypatch.setattr(
                service_manager, "_radarr_clients", {1: first, other_id: second}
            )

            with pytest.raises(DuplicateActionError, match="download it again"):
                await duplicate_file_ops.delete_duplicate_files(
                    DuplicateDeleteJobItem(
                        media_type=MediaType.MOVIE,
                        item_id=movie_id,
                        version_ids=[ids[0]],
                        display_label="Movie One",
                    ),
                    approved_by="t",
                )
            # the first instance passed its check but nothing was deleted
            assert first.deleted_file_ids == []
            assert second.deleted_file_ids == []
        finally:
            await engine.dispose()

    asyncio.run(run())


_SONARR_FILE = "/data/tv/Show/Season 01/Show - S01E01 - 720p.mkv"


async def _seed_sonarr_case(
    db: AsyncSession, *, kept_path: str
) -> tuple[int, list[int]]:
    db.add(GeneralSettings(media_server_fallback_enabled=False))
    config = ServiceConfig(
        service_type=Service.SONARR, base_url="http://sonarr", api_key="k", enabled=True
    )
    db.add(config)
    ep1, _ep2 = await _seed_episode(db)
    episode = await db.get(Episode, ep1)
    assert episode is not None
    episode.path = _SONARR_FILE
    db.add_all(
        [
            _episode_version(ep1, "a", path=_SONARR_FILE, size=500),
            _episode_version(
                ep1, "b", path=kept_path, size=900, video_height=2160, video_width=3840
            ),
        ]
    )
    await db.flush()
    season = await db.get(Season, episode.season_id)
    assert season is not None
    db.add(
        SeriesArrRef(
            series_id=season.series_id,
            service_config_id=config.id,
            arr_series_id=77,
            arr_series_path="/data/tv/Show",
        )
    )
    await db.commit()
    rows = (
        (await db.execute(select(EpisodeVersion).order_by(EpisodeVersion.id)))
        .scalars()
        .all()
    )
    return ep1, [r.id for r in rows]


def _episode_item(episode_id: int, version_ids: list[int]) -> DuplicateDeleteJobItem:
    return DuplicateDeleteJobItem(
        media_type=MediaType.SERIES,
        item_id=episode_id,
        version_ids=version_ids,
        display_label="Show S01E01",
    )


def test_job_deletes_episode_file_via_sonarr(monkeypatch) -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            kept = "/data/tv/Show/Season 01/Show - S01E01 - 2160p.mkv"
            async with sm() as db:
                episode_id, ids = await _seed_sonarr_case(db, kept_path=kept)
            sonarr = _FakeSonarr([{"id": 301, "path": _SONARR_FILE}])
            media = _FakeMediaServer()
            events = _patch(monkeypatch, sm, radarr=None, media=media, sonarr=sonarr)

            freed = await duplicate_file_ops.delete_duplicate_files(
                _episode_item(episode_id, [ids[0]]), approved_by="t"
            )

            assert freed == 500
            assert sonarr.deleted_file_ids == [301]
            assert media.deleted_versions == []
            assert len(events) == 1
            async with sm() as db:
                remaining = (await db.execute(select(EpisodeVersion))).scalars().all()
                assert [v.id for v in remaining] == [ids[1]]
                episode = await db.get(Episode, episode_id)
                # the episode now points at the kept file
                assert episode is not None and episode.path == kept
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_job_refuses_sonarr_delete_when_kept_copy_is_outside_its_folder(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                episode_id, ids = await _seed_sonarr_case(
                    db, kept_path="/data/tv-4k/Show/Season 01/Show - S01E01.mkv"
                )
            sonarr = _FakeSonarr([{"id": 301, "path": _SONARR_FILE}])
            _patch(
                monkeypatch, sm, radarr=None, media=_FakeMediaServer(), sonarr=sonarr
            )

            with pytest.raises(DuplicateActionError, match="download it again"):
                await duplicate_file_ops.delete_duplicate_files(
                    _episode_item(episode_id, [ids[0]]), approved_by="t"
                )
            assert sonarr.deleted_file_ids == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_delete_routes_require_manage_reclaim() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            viewer = User(
                username="viewer", password_hash="x", role=UserRole.USER, permissions=[]
            )
            manager = User(
                username="manager",
                password_hash="x",
                role=UserRole.USER,
                permissions=[Permission.MANAGE_RECLAIM.value],
            )
            body = DuplicateDeleteRequest(
                items=[
                    DuplicateDeleteItem(
                        media_type=MediaType.MOVIE, item_id=1, version_ids=[1]
                    )
                ]
            )
            async with sm() as db:
                with pytest.raises(HTTPException) as exc:
                    await delete_duplicates(body, viewer, db)
                assert exc.value.status_code == 403
                with pytest.raises(HTTPException) as exc:
                    await delete_leftovers(LeftoverDeleteRequest(ids=[1]), viewer, db)
                assert exc.value.status_code == 403

                # the permission check passes; the missing group is what fails
                with pytest.raises(HTTPException) as exc:
                    await delete_duplicates(body, manager, db)
                assert exc.value.status_code == 400
        finally:
            await engine.dispose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def _ev(media_id: str, path: str) -> EpisodeVersionData:
    return EpisodeVersionData(
        service=Service.JELLYFIN,
        service_item_id=f"item-{media_id}",
        service_media_id=media_id,
        library_id="L2",
        library_name="TV",
        path=path,
        size=100,
        added_at=datetime(2024, 1, 1),
    )


def test_sync_pools_versions_per_episode_and_prunes_on_replace() -> None:
    async def run() -> None:
        engine, sm = await _make_session()
        try:
            async with sm() as db:
                ep1, _ep2 = await _seed_episode(db)
                await db.commit()
                # Jellyfin reports an unmerged second copy as its own item
                data = [
                    AggregatedEpisodeData(
                        episode_number=1,
                        view_count=0,
                        versions=(_ev("a", "/tv/a.mkv"),),
                    ),
                    AggregatedEpisodeData(
                        episode_number=1,
                        view_count=0,
                        versions=(_ev("b", "/tv/b.mkv"),),
                    ),
                ]
                await _sync_episode_versions(db, {1: ep1}, data, replace=True)
                await db.commit()
                rows = (await db.execute(select(EpisodeVersion))).scalars().all()
                assert sorted(r.service_media_id for r in rows) == ["a", "b"]

                await _sync_episode_versions(db, {1: ep1}, data[:1], replace=True)
                await db.commit()
                rows = (await db.execute(select(EpisodeVersion))).scalars().all()
                assert [r.service_media_id for r in rows] == ["a"]

                # append never prunes
                await _sync_episode_versions(db, {1: ep1}, data[1:], replace=False)
                await db.commit()
                rows = (await db.execute(select(EpisodeVersion))).scalars().all()
                assert sorted(r.service_media_id for r in rows) == ["a", "b"]
        finally:
            await engine.dispose()

    asyncio.run(run())
