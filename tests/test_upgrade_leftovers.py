from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.service_manager import service_manager
from backend.database import Base
from backend.database.models import (
    GeneralSettings,
    Movie,
    MovieArrRef,
    ServiceConfig,
    UpgradeLeftover,
)
from backend.enums import Service
from backend.models.services.radarr import RadarrMovie
from backend.services import upgrade_leftovers
from backend.services.duplicates import DuplicateActionError
from backend.services.radarr import RadarrClient
from backend.services.upgrade_leftovers import (
    MANUAL_CURRENT_FILE_MISSING,
    MANUAL_NO_HARDLINK_INFO,
    MANUAL_NOT_A_FILE,
    ImportEvent,
    check_leftover,
    parse_import_events,
    split_imports,
)


def _record(movie_id: int, dropped: str, date: str) -> dict[str, Any]:
    return {
        "movieId": movie_id,
        "eventType": "downloadFolderImported",
        "date": date,
        "sourceTitle": Path(dropped).stem,
        "data": {"droppedPath": dropped, "importedPath": f"/movies/{movie_id}.mkv"},
    }


def _event(movie_id: int, dropped: str, day: int) -> ImportEvent:
    return ImportEvent(movie_id, dropped, datetime(2026, 1, day), None)


# ---------------------------------------------------------------------------
# Radarr client
# ---------------------------------------------------------------------------


def test_import_history_pages_and_filters_event_type() -> None:
    async def run() -> None:
        client = RadarrClient(api_key="key", base_url="http://radarr")
        pages = [
            (
                200,
                {
                    "totalRecords": 3,
                    "records": [
                        _record(1, "/dl/a.mkv", "2026-01-02T00:00:00Z"),
                        {"eventType": "grabbed", "movieId": 1},
                    ],
                },
            ),
            (200, {"totalRecords": 3, "records": [_record(2, "/dl/b.mkv", "")]}),
        ]
        request = AsyncMock(side_effect=pages)
        try:
            with patch.object(RadarrClient, "_make_request", request):
                records = await client.get_import_history(page_size=2)
        finally:
            await client.session.close()

        assert [r["movieId"] for r in records] == [1, 2]
        assert request.await_count == 2
        assert request.await_args_list[1].kwargs["params"]["page"] == 2
        assert request.await_args_list[0].kwargs["params"]["eventType"] == 3

    asyncio.run(run())


def test_import_history_rejects_non_paged_response() -> None:
    async def run() -> None:
        client = RadarrClient(api_key="key", base_url="http://radarr")
        request = AsyncMock(return_value=(200, []))
        try:
            with patch.object(RadarrClient, "_make_request", request):
                with pytest.raises(ValueError, match="import history page 1"):
                    await client.get_import_history()
        finally:
            await client.session.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# pure detection
# ---------------------------------------------------------------------------


def test_parse_import_events_skips_records_without_movie_or_path() -> None:
    events = parse_import_events(
        [
            _record(1, "/dl/a.mkv", "2026-01-02T05:00:00Z"),
            {"movieId": 2, "data": {}},
            {"movieId": 0, "data": {"droppedPath": "/dl/x.mkv"}},
            {"movieId": 3, "data": "nope"},
        ]
    )
    assert events == [ImportEvent(1, "/dl/a.mkv", datetime(2026, 1, 2, 5), "a")]


def test_split_imports_keeps_latest_and_its_source_out() -> None:
    events = [
        _event(1, "/dl/new.mkv", 9),
        _event(1, "/dl/old.mkv", 3),
        _event(1, "/dl/old.mkv", 2),  # same file imported twice
        _event(1, "/dl/new.mkv", 1),  # the current file's source, imported earlier
        _event(2, "/dl/only.mkv", 5),  # single import: nothing superseded
    ]
    latest, superseded = split_imports(events)
    assert latest == [_event(1, "/dl/new.mkv", 9), _event(2, "/dl/only.mkv", 5)]
    assert superseded == [_event(1, "/dl/old.mkv", 3)]


def test_check_leftover_skips_hardlink_of_current_file(tmp_path: Path) -> None:
    current = tmp_path / "library.mkv"
    current.write_bytes(b"x" * 10)
    seed = tmp_path / "download.mkv"
    os.link(current, seed)
    assert check_leftover(_event(1, "/dl/x", 1), seed, current) is None


def test_check_leftover_reports_size_and_links(tmp_path: Path) -> None:
    current = tmp_path / "library.mkv"
    current.write_bytes(b"new")
    old = tmp_path / "old.mkv"
    old.write_bytes(b"x" * 10)

    single = check_leftover(_event(1, "/dl/x", 1), old, current)
    assert single is not None
    assert (single.size, single.link_count, single.manual_reason) == (10, 1, None)

    os.link(old, tmp_path / "elsewhere.mkv")
    linked = check_leftover(_event(1, "/dl/x", 1), old, current)
    assert linked is not None
    assert (linked.link_count, linked.manual_reason) == (2, None)


def test_check_leftover_manual_cases(tmp_path: Path) -> None:
    old = tmp_path / "old.mkv"
    old.write_bytes(b"x")
    folder = tmp_path / "Release"
    folder.mkdir()

    no_current = check_leftover(_event(1, "/dl/x", 1), old, None)
    assert no_current is not None
    assert no_current.manual_reason == MANUAL_CURRENT_FILE_MISSING

    is_folder = check_leftover(_event(1, "/dl/x", 1), folder, old)
    assert is_folder is not None
    assert is_folder.manual_reason == MANUAL_NOT_A_FILE

    assert check_leftover(_event(1, "/dl/x", 1), tmp_path / "gone.mkv", old) is None


class _NoInodePath:
    """A file on a share that reports inode 0, like some SMB mounts."""

    def stat(self) -> os.stat_result:
        return os.stat_result((0o100644, 0, 0, 1, 0, 0, 5, 0, 0, 0))


def test_check_leftover_flags_filesystems_without_inodes(tmp_path: Path) -> None:
    real = tmp_path / "real.mkv"
    real.write_bytes(b"x")

    no_inode = check_leftover(_event(1, "/dl/x", 1), _NoInodePath(), real)  # type: ignore[arg-type]
    assert no_inode is not None
    assert no_inode.manual_reason == MANUAL_NO_HARDLINK_INFO

    current_no_inode = check_leftover(_event(1, "/dl/x", 1), real, _NoInodePath())  # type: ignore[arg-type]
    assert current_no_inode is not None
    assert current_no_inode.manual_reason == MANUAL_NO_HARDLINK_INFO


# ---------------------------------------------------------------------------
# full scan
# ---------------------------------------------------------------------------


class _FakeRadarr:
    def __init__(self, movies: list[RadarrMovie], history: list[dict[str, Any]]):
        self.movies = movies
        self.history = history
        self.fail = False

    async def get_all_movies(self) -> list[RadarrMovie]:
        if self.fail:
            raise ConnectionError("radarr down")
        return self.movies

    async def get_import_history(self) -> list[dict[str, Any]]:
        return self.history


def _radarr_movie(movie_id: int, file_path: str | None) -> RadarrMovie:
    return RadarrMovie(
        id=movie_id,
        title=f"Movie {movie_id}",
        title_slug=None,
        tmdb_id=None,
        imdb_id=None,
        year=2020,
        path=f"/movies/Movie {movie_id}",
        has_file=file_path is not None,
        monitored=True,
        tags=[],
        file_path=file_path,
    )


def test_scan_flags_only_real_leftovers_and_keeps_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = tmp_path / "downloads" / "radarr"
    library = tmp_path / "movies"
    (library / "Movie 2").mkdir(parents=True)
    downloads.mkdir(parents=True)
    (library / "one.mkv").write_bytes(b"new")
    (downloads / "one-old.mkv").write_bytes(b"old" * 10)
    os.link(library / "one.mkv", downloads / "one-new.mkv")
    # the other instance's current download, once imported by movie 1 too
    (downloads / "cross.mkv").write_bytes(b"cross")
    # an old import from inside a movie folder
    (library / "Movie 2" / "in-place.mkv").write_bytes(b"lib")

    mappings = [
        {"source_prefix": "/downloads", "local_prefix": str(tmp_path / "downloads")},
        {"source_prefix": "/movies", "local_prefix": str(library)},
    ]
    radarr_a = _FakeRadarr(
        [_radarr_movie(1, "/movies/one.mkv"), _radarr_movie(2, "/movies/two.mkv")],
        [
            _record(1, "/downloads/radarr/one-new.mkv", "2026-02-01T00:00:00Z"),
            _record(1, "/downloads/radarr/one-old.mkv", "2026-01-01T00:00:00Z"),
            _record(1, "/downloads/radarr/one-gone.mkv", "2025-12-01T00:00:00Z"),
            _record(1, "/downloads/radarr/cross.mkv", "2025-11-01T00:00:00Z"),
            _record(1, "/elsewhere/cat/rel/one.mkv", "2025-10-01T00:00:00Z"),
            _record(2, "/downloads/radarr/two.mkv", "2026-02-01T00:00:00Z"),
            _record(2, "/movies/Movie 2/in-place.mkv", "2026-01-01T00:00:00Z"),
            # movie 9 is no longer in Radarr
            _record(9, "/downloads/radarr/x-new.mkv", "2026-02-01T00:00:00Z"),
            _record(9, "/downloads/radarr/one-old.mkv", "2026-01-01T00:00:00Z"),
        ],
    )
    radarr_b = _FakeRadarr(
        [_radarr_movie(5, "/movies/five.mkv")],
        [_record(5, "/downloads/radarr/cross.mkv", "2026-02-01T00:00:00Z")],
    )

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(upgrade_leftovers, "async_db", sm)
        monkeypatch.setattr(service_manager, "_radarr_clients", {})

        async def rows() -> list[UpgradeLeftover]:
            async with sm() as db:
                return list((await db.execute(select(UpgradeLeftover))).scalars())

        try:
            async with sm() as db:
                configs = [
                    ServiceConfig(
                        service_type=Service.RADARR,
                        base_url=f"http://radarr-{name}",
                        name=name,
                        api_key="k",
                        enabled=True,
                    )
                    for name in ("a", "b")
                ]
                movie = Movie(title="Movie One", tmdb_id=101, year=2020)
                db.add_all([*configs, movie, GeneralSettings(path_mappings=mappings)])
                await db.flush()
                db.add(
                    MovieArrRef(
                        movie_id=movie.id,
                        service_config_id=configs[0].id,
                        arr_movie_id=1,
                    )
                )
                await db.commit()
                config_a, config_b, movie_id = configs[0].id, configs[1].id, movie.id
            service_manager._radarr_clients[config_a] = radarr_a  # type: ignore[assignment]
            service_manager._radarr_clients[config_b] = radarr_b  # type: ignore[assignment]

            result = await upgrade_leftovers.scan_upgrade_leftovers()
            assert result == {"leftovers": 1, "unmapped_roots": 1}
            (row,) = await rows()
            assert row.dropped_path == "/downloads/radarr/one-old.mkv"
            assert (row.size, row.link_count, row.movie_id) == (30, 1, movie_id)
            assert row.manual_reason is None
            async with sm() as db:
                settings = (await db.execute(select(GeneralSettings))).scalar_one()
                assert settings.upgrade_leftover_scan is not None
                assert settings.upgrade_leftover_scan["unmapped_roots"] == [
                    "/elsewhere"
                ]
                stored = await db.get(UpgradeLeftover, row.id)
                assert stored is not None
                stored.ignored = True
                await db.commit()

            # a rescan keeps the ignore flag
            await upgrade_leftovers.scan_upgrade_leftovers()
            (row,) = await rows()
            assert row.ignored is True

            # a different file at the same path clears it (made before the swap,
            # so it can't reuse the old inode)
            (downloads / "one-old.tmp").write_bytes(b"new" * 10)
            os.replace(downloads / "one-old.tmp", downloads / "one-old.mkv")
            await upgrade_leftovers.scan_upgrade_leftovers()
            (row,) = await rows()
            assert row.ignored is False

            # a failing instance stops the scan and keeps previous results
            (downloads / "one-old.mkv").unlink()
            radarr_b.fail = True
            with pytest.raises(RuntimeError, match="previous results kept"):
                await upgrade_leftovers.scan_upgrade_leftovers()
            assert len(await rows()) == 1

            # a deleted file drops the row
            radarr_b.fail = False
            await upgrade_leftovers.scan_upgrade_leftovers()
            assert await rows() == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_route_filters_and_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.api.routes import duplicates as routes
    from backend.core import task_runtime

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(task_runtime, "async_db", sm)
        try:
            async with sm() as db:
                config = ServiceConfig(
                    service_type=Service.RADARR,
                    base_url="http://radarr",
                    api_key="k",
                    enabled=True,
                )
                db.add(config)
                db.add(
                    GeneralSettings(
                        upgrade_leftover_scan={
                            "scanned_at": "2026-09-23T09:00:00+00:00",
                            "unmapped_roots": ["/downloads/radarr"],
                        }
                    )
                )
                await db.flush()

                def leftover(title: str, size: int, **extra: Any) -> UpgradeLeftover:
                    row = UpgradeLeftover(
                        service_config_id=config.id,
                        arr_movie_id=1,
                        title=title,
                        dropped_path=f"/dl/{title}.mkv",
                        local_path=f"/local/{title}.mkv",
                        size=size,
                        link_count=extra.pop("link_count", 1),
                    )
                    for key, value in extra.items():
                        setattr(row, key, value)
                    return row

                db.add_all(
                    [
                        leftover("Bravo", 100),
                        leftover("Alpha", 10, link_count=2),  # frees no space
                        leftover("Charlie", 50, manual_reason="odd"),
                        leftover("Delta", 70, ignored=True),
                    ]
                )
                await db.commit()

                async def listing(**overrides: Any) -> Any:
                    params: dict[str, Any] = {
                        "page": 1,
                        "per_page": 25,
                        "search": None,
                        "sort_by": "title",
                        "include_manual": True,
                        "include_ignored": False,
                    }
                    params.update(overrides)
                    return await routes.list_leftovers(None, db, **params)  # type: ignore[arg-type]

                result = await listing()
                assert [i.title for i in result.items] == ["Alpha", "Bravo", "Charlie"]
                assert result.items[0].frees_space is False
                assert (result.summary.actionable, result.summary.reclaimable_size) == (
                    2,
                    100,
                )
                assert result.scan.unmapped_roots == ["/downloads/radarr"]
                assert result.scan.scanned_at == datetime(2026, 9, 23, 9)
                assert result.scan.running is False

                by_size = await listing(sort_by="size", include_manual=False)
                assert [i.title for i in by_size.items] == ["Bravo", "Alpha"]

                everything = await listing(include_ignored=True, search="elt")
                assert [i.title for i in everything.items] == ["Delta"]
        finally:
            await engine.dispose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# deleting
# ---------------------------------------------------------------------------


def test_remove_release_folder_only_takes_release_folders_with_junk_left(
    tmp_path: Path,
) -> None:
    release = tmp_path / "Movie.2020.1080p-GRP"
    (release / "Sample").mkdir(parents=True)
    (release / "Sample" / "sample.mkv").write_bytes(b"s")
    (release / "movie.nfo").write_text("nfo")
    assert upgrade_leftovers.remove_release_folder(release, ["Movie.2020.1080p-GRP"])
    assert not release.exists()

    # a shared category folder is never removed, even when it looks empty-ish
    category = tmp_path / "radarr"
    category.mkdir()
    (category / "left.nfo").write_text("nfo")
    assert not upgrade_leftovers.remove_release_folder(category, ["Other.Release"])
    assert category.exists()

    # another video still in the folder keeps it
    busy = tmp_path / "Busy.Release"
    busy.mkdir()
    (busy / "other.mkv").write_bytes(b"v")
    assert not upgrade_leftovers.remove_release_folder(busy, ["busy.release"])
    assert busy.exists()


def _row(local: Path, **overrides: Any) -> UpgradeLeftover:
    st = local.stat()
    row = UpgradeLeftover(
        service_config_id=1,
        arr_movie_id=1,
        title="Movie 1",
        dropped_path="/downloads/radarr/Old.Release/old.mkv",
        local_path=str(local),
        size=st.st_size,
        link_count=st.st_nlink,
        file_key=upgrade_leftovers.file_key(st),
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _delete_setup(tmp_path: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    release = tmp_path / "downloads" / "radarr" / "Old.Release"
    release.mkdir(parents=True)
    old = release / "old.mkv"
    old.write_bytes(b"old" * 10)
    (release / "old.nfo").write_text("nfo")
    library = tmp_path / "movies"
    library.mkdir()
    (library / "one.mkv").write_bytes(b"new")
    mappings = [
        {"source_prefix": "/downloads", "local_prefix": str(tmp_path / "downloads")},
        {"source_prefix": "/movies", "local_prefix": str(library)},
    ]
    return old, library / "one.mkv", mappings


def test_check_before_delete_refuses_changed_state(tmp_path: Path) -> None:
    old, current, mappings = _delete_setup(tmp_path)
    movie = _radarr_movie(1, "/movies/one.mkv")
    assert upgrade_leftovers.check_before_delete(_row(old), movie, mappings)

    def refused(row: UpgradeLeftover, match: str, m: RadarrMovie = movie) -> None:
        with pytest.raises(DuplicateActionError, match=match):
            upgrade_leftovers.check_before_delete(row, m, mappings)

    refused(_row(old, manual_reason="odd"), "manual review")
    refused(_row(old, file_key="1:2"), "changed since the scan")
    refused(
        _row(old, dropped_path="/movies/Movie 1/old.mkv"), "inside the movie's library"
    )
    # Radarr re-imported the leftover: it is now the current file
    current.unlink()
    os.link(old, current)
    refused(_row(old), "Radarr now uses this file")
    gone = _row(old)
    old.unlink()
    current.unlink()
    refused(gone, "already gone")


def test_check_before_delete_handles_current_file_vanishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, _current, mappings = _delete_setup(tmp_path)
    movie = _radarr_movie(1, "/movies/one.mkv")
    # the current file resolves, then disappears before it is compared
    monkeypatch.setattr(
        upgrade_leftovers, "resolve_path", lambda *_a, **_k: tmp_path / "gone.mkv"
    )
    with pytest.raises(DuplicateActionError, match="current movie file"):
        upgrade_leftovers.check_before_delete(_row(old), movie, mappings)


class _RadarrForDelete:
    def __init__(self, movie: RadarrMovie) -> None:
        self.movie = movie

    async def get_movie(self, movie_id: int) -> RadarrMovie:
        assert movie_id == self.movie.id
        return self.movie


def test_delete_job_removes_file_folder_row_and_records_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.database.models import ReclaimHistory
    from backend.jobs import duplicate_file_ops
    from backend.models.jobs import DuplicateDeleteJobPayload, LeftoverDeleteJobItem

    old, _current, mappings = _delete_setup(tmp_path)

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(duplicate_file_ops, "async_db", sm)
        monkeypatch.setattr(
            duplicate_file_ops, "update_background_job_payload", AsyncMock()
        )
        events: list[dict[str, Any]] = []

        async def _record(**kwargs: Any) -> None:
            events.append(kwargs)

        monkeypatch.setattr(duplicate_file_ops, "_dispatch_reclaim_event", _record)
        try:
            async with sm() as db:
                config = ServiceConfig(
                    service_type=Service.RADARR,
                    base_url="http://radarr",
                    api_key="k",
                    enabled=True,
                )
                db.add_all([config, GeneralSettings(path_mappings=mappings)])
                await db.flush()
                row = _row(old, service_config_id=config.id, source_title="Old.Release")
                db.add(row)
                await db.commit()
                config_id, row_id = config.id, row.id
            monkeypatch.setattr(
                service_manager,
                "_radarr_clients",
                {config_id: _RadarrForDelete(_radarr_movie(1, "/movies/one.mkv"))},
            )

            result = await duplicate_file_ops.run_duplicate_delete_job(
                1,
                DuplicateDeleteJobPayload(
                    items=[],
                    leftovers=[
                        LeftoverDeleteJobItem(id=row_id, display_label="Movie 1"),
                        LeftoverDeleteJobItem(id=999, display_label="Gone"),
                    ],
                    requested_by_user_id=1,
                    requested_by_username="admin",
                ),
            )
            assert (result["succeeded"], result["failed"]) == (1, 1)
            assert result["freed_bytes"] == 30
            assert "Gone: No longer listed" in result["errors"][0]
            assert not old.parent.exists()
            async with sm() as db:
                assert (await db.execute(select(UpgradeLeftover))).first() is None
                history = (await db.execute(select(ReclaimHistory))).scalar_one()
                assert (history.size, history.attributes) == (
                    30,
                    {"source": "upgrade_leftover"},
                )
            assert events[0]["path"] == "/downloads/radarr/Old.Release/old.mkv"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("path", "root"),
    [
        ("/downloads/radarr_dl/Release/movie.mkv", "/downloads"),
        ("/downloads/radarr_dl/movie.mkv", "/downloads"),
        (r"D:\Downloads\radarr\movie.mkv", "D:/Downloads"),
        (r"\\nas\share\dl\movie.mkv", "//nas/share"),
    ],
)
def test_download_root_is_stable_across_depths(path: str, root: str) -> None:
    assert upgrade_leftovers.download_root(path) == root
