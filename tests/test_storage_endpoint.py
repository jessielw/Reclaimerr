from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes import storage as storage_route
from backend.database import Base
from backend.database.models import (
    GeneralSettings,
    Movie,
    ReclaimCandidate,
    ReclaimHistory,
    Series,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.models.storage import StorageMountTarget, StorageMountUpdate
from backend.services.arr_disk import ArrDiskError, ArrDiskSpace

GB = 1024**3


def _user() -> User:
    return User(username="admin", password_hash="x", role=UserRole.ADMIN)


def _entry(
    path: str,
    *,
    total: int | None,
    free: int | None,
    service_type: Service = Service.RADARR,
    config_id: int = 1,
    label: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": path,
        "service_type": service_type.value,
        "service_config_id": config_id,
    }
    # mirrors what RadarrClient/SonarrClient.get_disk_space() returns: a total
    # of 0 is how an Arr says "this mount has no reportable total"
    entry["total_space"] = total or 0
    entry["free_space"] = free or 0
    entry["label"] = label or ""
    return entry


def _patch_disk(
    monkeypatch: pytest.MonkeyPatch,
    entries: list[dict[str, Any]],
    errors: list[ArrDiskError] | None = None,
) -> None:
    async def _fake() -> ArrDiskSpace:
        return ArrDiskSpace(entries=entries, errors=errors or [])

    monkeypatch.setattr(storage_route, "load_arr_disk_space", _fake)


async def _session() -> tuple[AsyncSession, Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    return session_maker(), engine


def _radarr_config(config_id: int, name: str) -> ServiceConfig:
    config = ServiceConfig(
        service_type=Service.RADARR,
        name=name,
        base_url="http://radarr.invalid",
        api_key="k",
        enabled=True,
    )
    config.id = config_id
    return config


@pytest.mark.anyio
async def test_capacity_adds_up_across_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_disk(
        monkeypatch,
        [
            _entry("/movies", total=100 * GB, free=40 * GB, label="Movies"),
            _entry("/tv", total=200 * GB, free=50 * GB, config_id=2),
        ],
    )
    db, engine = await _session()
    async with db:
        db.add(_radarr_config(1, "Radarr 4K"))
        await db.flush()
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()

    assert response.capacity_total_bytes == 300 * GB
    assert response.capacity_free_bytes == 90 * GB
    assert response.capacity_used_bytes == 210 * GB
    assert response.capacity_incomplete is False
    assert [mount.path for mount in response.mounts] == ["/movies", "/tv"]
    assert response.mounts[0].label == "Movies"
    assert response.mounts[0].sources[0].name == "Radarr 4K"


@pytest.mark.anyio
async def test_one_volume_reported_twice_is_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Radarr and Sonarr sharing a mount must not double the capacity
    _patch_disk(
        monkeypatch,
        [
            _entry("/data", total=100 * GB, free=25 * GB, config_id=1),
            _entry(
                "/data",
                total=100 * GB,
                free=25 * GB,
                service_type=Service.SONARR,
                config_id=1,
            ),
        ],
    )
    db, engine = await _session()
    async with db:
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()

    assert len(response.mounts) == 1
    assert response.capacity_total_bytes == 100 * GB
    assert {source.service_type for source in response.mounts[0].sources} == {
        "radarr",
        "sonarr",
    }
    # each instance still reports its own view
    assert len(response.instances) == 2


@pytest.mark.anyio
async def test_the_same_path_on_different_disks_stays_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_disk(
        monkeypatch,
        [
            _entry("/data", total=100 * GB, free=10 * GB, config_id=1),
            _entry(
                "/data",
                total=500 * GB,
                free=20 * GB,
                service_type=Service.SONARR,
                config_id=1,
            ),
        ],
    )
    db, engine = await _session()
    async with db:
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()

    assert len(response.mounts) == 2
    assert response.capacity_total_bytes == 600 * GB


@pytest.mark.anyio
async def test_a_mount_without_a_total_is_flagged_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_disk(
        monkeypatch,
        [
            _entry("/movies", total=100 * GB, free=40 * GB),
            _entry("/remote", total=None, free=5 * GB, config_id=2),
        ],
    )
    db, engine = await _session()
    async with db:
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()

    assert response.capacity_incomplete is True
    assert response.capacity_total_bytes == 100 * GB
    # free space is still counted, so the free figure includes the remote mount
    assert response.capacity_free_bytes == 45 * GB
    remote = next(mount for mount in response.mounts if mount.path == "/remote")
    assert remote.used_bytes is None


@pytest.mark.anyio
async def test_an_unreachable_instance_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_disk(
        monkeypatch,
        [_entry("/movies", total=100 * GB, free=40 * GB)],
        [ArrDiskError(service_type="sonarr", service_config_id=3, message="timeout")],
    )
    db, engine = await _session()
    async with db:
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()

    assert response.capacity_total_bytes == 100 * GB
    assert len(response.errors) == 1
    assert "timeout" in response.errors[0]


@pytest.mark.anyio
async def test_library_totals_exclude_removed_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    _patch_disk(monkeypatch, [])
    db, engine = await _session()
    async with db:
        removed = Movie(title="Gone", tmdb_id=2, size=50)
        removed.removed_at = datetime.now(UTC)
        db.add_all(
            [
                Movie(title="Here", tmdb_id=1, size=100),
                removed,
                Series(title="Show", tmdb_id=3, size=300),
            ]
        )
        await db.flush()
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()

    by_type = {entry.media_type: entry for entry in response.libraries}
    assert by_type["movie"].item_count == 1
    assert by_type["movie"].total_bytes == 100
    assert by_type["series"].total_bytes == 300
    assert response.library_total_bytes == 400


@pytest.mark.anyio
async def test_reclaimable_and_reclaimed_are_grouped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_disk(monkeypatch, [])
    db, engine = await _session()
    async with db:
        movie_candidate = ReclaimCandidate(
            media_type=MediaType.MOVIE,
            matched_rule_ids=[1],
            matched_criteria={},
            reason="test",
            movie_id=1,
        )
        movie_candidate.estimated_space_bytes = 700
        series_candidate = ReclaimCandidate(
            media_type=MediaType.SERIES,
            matched_rule_ids=[1],
            matched_criteria={},
            reason="test",
            series_id=1,
        )
        series_candidate.estimated_space_bytes = 300
        db.add_all(
            [
                movie_candidate,
                series_candidate,
                ReclaimHistory(
                    approved_by="system",
                    media_type=MediaType.MOVIE,
                    tmdb_id=1,
                    size=1000,
                    action="deleted",
                ),
                ReclaimHistory(
                    approved_by="system",
                    media_type=MediaType.MOVIE,
                    tmdb_id=2,
                    size=500,
                    action="moved",
                ),
                ReclaimHistory(
                    approved_by="system",
                    media_type=MediaType.SERIES,
                    tmdb_id=3,
                    size=250,
                    action="deleted",
                ),
            ]
        )
        await db.flush()
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()

    assert response.reclaimable_total_bytes == 1000
    reclaimable = {entry.media_type: entry for entry in response.reclaimable}
    assert reclaimable["movie"].candidate_count == 1
    assert reclaimable["movie"].total_bytes == 700

    assert response.reclaimed_total_bytes == 1750
    outcomes = {
        (entry.media_type, entry.action): entry.total_bytes
        for entry in response.reclaimed
    }
    assert outcomes[("movie", "deleted")] == 1000
    assert outcomes[("movie", "moved")] == 500
    assert outcomes[("series", "deleted")] == 250


@pytest.mark.anyio
async def test_an_instance_with_no_config_row_still_gets_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_disk(monkeypatch, [_entry("/movies", total=10 * GB, free=1 * GB)])
    db, engine = await _session()
    async with db:
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()

    assert response.instances[0].name == "Radarr"


@pytest.mark.anyio
async def test_alias_paths_are_visible_but_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        _entry(path, total=1830 * GB, free=500 * GB, service_type=service)
        for path in ("/", "/config", "/media")
        for service in (Service.RADARR, Service.SONARR)
    ]
    # Repeated reports from one Arr must not multiply its own totals either.
    _patch_disk(monkeypatch, entries + [entries[0]])
    db, engine = await _session()
    async with db:
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()

    assert response.capacity_total_bytes == 1830 * GB
    assert response.capacity_free_bytes == 500 * GB
    assert response.capacity_used_bytes == 1330 * GB
    assert len(response.mounts) == 3
    assert [m.path for m in response.mounts if m.included_in_capacity] == ["/"]
    assert [m.duplicate_of for m in response.mounts] == [None, "/", "/"]
    assert all(len(m.sources) == 2 for m in response.mounts)
    assert all(i.total_bytes == 1830 * GB for i in response.instances)
    assert all(i.free_bytes == 500 * GB for i in response.instances)
    assert all(i.mount_count == 1 for i in response.instances)


@pytest.mark.anyio
async def test_pooled_storage_choices_persist_when_space_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def entries(free: int) -> list[dict[str, Any]]:
        return [
            _entry(path, total=total * GB, free=free, service_type=service)
            for path, total in (
                ("/", 1830),
                ("/config", 1830),
                ("/media", 1830),
                ("/media/nas", 13960),
                ("/media/mediamerge", 15790),
            )
            for service in (Service.RADARR, Service.SONARR)
        ]

    _patch_disk(monkeypatch, entries(500 * GB))
    db, engine = await _session()
    async with db:
        response = await storage_route.get_storage(_user=_user(), db=db)
        assert response.capacity_total_bytes == (1830 + 13960 + 15790) * GB
        # Keep the pool; exclude every branch/alias reported by both Arrs.
        await storage_route.update_storage_mounts(
            StorageMountUpdate(
                targets=[
                    StorageMountTarget(
                        service_type=source.service_type,
                        service_config_id=source.service_config_id,
                        path=mount.path,
                    )
                    for mount in response.mounts
                    if mount.path != "/media/mediamerge"
                    for source in mount.sources
                ],
                mode="exclude",
            ),
            _admin=_user(),
            db=db,
        )
    # Read through a fresh session, with different free-space snapshots.
    _patch_disk(monkeypatch, entries(400 * GB))
    async with AsyncSession(engine) as fresh:
        response = await storage_route.get_storage(_user=_user(), db=fresh)
    await engine.dispose()

    assert response.capacity_total_bytes == 15790 * GB
    assert response.capacity_free_bytes == 400 * GB
    assert response.capacity_used_bytes == (15790 - 400) * GB
    assert all(i.total_bytes == 15790 * GB for i in response.instances)
    assert all(i.mount_count == 1 for i in response.instances)
    assert len(response.mounts) == 5


@pytest.mark.anyio
async def test_include_can_override_duplicate_guess_and_auto_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_disk(
        monkeypatch,
        [
            _entry("/a", total=100 * GB, free=40 * GB),
            _entry("/b", total=100 * GB, free=40 * GB),
        ],
    )
    db, engine = await _session()
    async with db:
        targets = [
            StorageMountTarget(service_type="radarr", service_config_id=1, path=p)
            for p in ("/a", "/b")
        ]
        await storage_route.update_storage_mounts(
            StorageMountUpdate(targets=targets, mode="include"),
            _admin=_user(),
            db=db,
        )
        included = await storage_route.get_storage(_user=_user(), db=db)
        assert included.capacity_total_bytes == 200 * GB
        assert included.instances[0].total_bytes == 200 * GB
        await storage_route.update_storage_mounts(
            StorageMountUpdate(targets=targets, mode="auto"),
            _admin=_user(),
            db=db,
        )
        automatic = await storage_route.get_storage(_user=_user(), db=db)
        assert automatic.capacity_total_bytes == 100 * GB
        assert all(m.capacity_mode == "auto" for m in automatic.mounts)
        settings = await db.get(GeneralSettings, 1)
        assert settings is not None and settings.storage_mount_overrides == {}
    await engine.dispose()


@pytest.mark.anyio
async def test_unknown_or_different_snapshots_are_not_automatic_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        _entry("/a", total=100 * GB, free=40 * GB),
        _entry("/b", total=100 * GB, free=30 * GB),
        _entry("/remote-a", total=None, free=5 * GB),
        _entry("/remote-b", total=None, free=5 * GB),
        _entry("/unknown-a", total=100 * GB, free=None),
        _entry("/unknown-b", total=100 * GB, free=None),
    ]
    for entry in entries[-2:]:
        entry.pop("free_space")
    _patch_disk(monkeypatch, entries)
    db, engine = await _session()
    async with db:
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()
    assert response.capacity_total_bytes == 400 * GB
    assert response.capacity_free_bytes == 80 * GB
    assert response.capacity_incomplete is True
    assert all(m.included_in_capacity for m in response.mounts)


@pytest.mark.anyio
async def test_excluded_unknown_mount_does_not_mark_capacity_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_disk(monkeypatch, [_entry("/remote", total=None, free=5 * GB)])
    db, engine = await _session()
    async with db:
        await storage_route.update_storage_mounts(
            StorageMountUpdate(
                targets=[
                    StorageMountTarget(
                        service_type="radarr", service_config_id=1, path="/remote"
                    )
                ],
                mode="exclude",
            ),
            _admin=_user(),
            db=db,
        )
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()
    assert response.capacity_total_bytes == response.capacity_free_bytes == 0
    assert response.capacity_incomplete is False
    assert response.instances[0].mount_count == 0


@pytest.mark.anyio
async def test_each_instance_uses_its_own_free_space_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_disk(
        monkeypatch,
        [
            _entry("/data", total=100 * GB, free=40 * GB),
            _entry("/data", total=100 * GB, free=30 * GB, service_type=Service.SONARR),
            _entry("/alias", total=100 * GB, free=30 * GB, service_type=Service.SONARR),
        ],
    )
    db, engine = await _session()
    async with db:
        response = await storage_route.get_storage(_user=_user(), db=db)
    await engine.dispose()
    instances = {i.service_type: i for i in response.instances}
    assert instances["radarr"].free_bytes == 40 * GB
    assert instances["sonarr"].free_bytes == 30 * GB
    assert instances["sonarr"].used_bytes == 70 * GB
    assert all(i.mount_count == 1 for i in response.instances)


def test_mount_choices_require_admin() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.core.auth import get_current_user

    app = FastAPI()
    app.include_router(storage_route.router)
    app.dependency_overrides[get_current_user] = lambda: User(
        username="viewer",
        password_hash="x",
        role=UserRole.USER,
    )
    # Do not open the application's database even for rejected requests.
    app.dependency_overrides[storage_route.get_db] = lambda: None
    with TestClient(app) as client:
        response = client.put(
            "/api/storage/mounts",
            json={
                "mode": "exclude",
                "targets": [
                    {"service_type": "radarr", "service_config_id": 1, "path": "/"}
                ],
            },
        )
    assert response.status_code == 403


def test_storage_choices_migration_preserves_existing_settings() -> None:
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    migration = importlib.import_module(
        "backend.alembic.versions.f1a6c8d3e205_add_storage_mount_overrides"
    )
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE general_settings (id INTEGER PRIMARY KEY)")
        )
        connection.execute(text("INSERT INTO general_settings (id) VALUES (1)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert (
                connection.execute(
                    text(
                        "SELECT storage_mount_overrides FROM general_settings WHERE id = 1"
                    )
                ).scalar_one()
                == "{}"
            )
            migration.downgrade()
        assert [
            c["name"] for c in inspect(connection).get_columns("general_settings")
        ] == ["id"]
        assert (
            connection.execute(text("SELECT id FROM general_settings")).scalar_one()
            == 1
        )
    engine.dispose()
