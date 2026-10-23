from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.settings import backups as backup_routes
from backend.core.auth import get_current_user
from backend.database import alembic_config
from backend.database.models import User
from backend.enums import UserRole
from backend.services import database_backup
from backend.services.database_backup import (
    InvalidBackupError,
    apply_pending_restore,
    cancel_pending_restore,
    copy_database,
    has_pending_restore,
    list_backups,
    pending_restore_path,
    prune_backups,
    resolve_backup,
    stage_restore,
    write_backup,
)


def _make_source(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE rules (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO rules (name) VALUES ('keep favorites')")
    conn.commit()
    conn.close()


def _tables(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
    finally:
        conn.close()


def test_copy_is_a_valid_database_with_the_same_tables(tmp_path: Path) -> None:
    source = tmp_path / "reclaimerr.db"
    _make_source(source)
    # a writer holding the WAL open must not stop the copy or hide its rows
    writer = sqlite3.connect(source)
    writer.execute("INSERT INTO rules (name) VALUES ('second')")
    writer.commit()

    target = tmp_path / "copy.db"
    try:
        copy_database(target, source=source)
    finally:
        writer.close()

    assert _tables(target) == _tables(source) == ["history", "rules"]
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM rules").fetchone() == (2,)
    finally:
        conn.close()


def test_retention_deletes_only_the_oldest_backups(tmp_path: Path) -> None:
    names = [f"reclaimerr-2026010{day}-000000.db" for day in range(1, 6)]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    unrelated = ["notes.db", "reclaimerr.db", f"{names[0]}.partial"]
    for name in unrelated:
        (tmp_path / name).write_bytes(b"")

    removed = prune_backups(2, tmp_path)

    assert sorted(removed) == names[:3]
    remaining = sorted(path.name for path in tmp_path.iterdir())
    assert remaining == sorted([*names[3:], *unrelated])


def test_scheduled_backup_writes_a_named_copy_and_applies_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database_backup.settings, "data_dir", tmp_path / "data")
    source = database_backup.settings.db_path
    _make_source(source)
    folder = tmp_path / "backups"
    folder.mkdir()
    for day in (1, 2):
        (folder / f"reclaimerr-2026010{day}-000000.db").write_bytes(b"")

    path = write_backup(2, folder)

    assert [item.name for item in list_backups(folder)] == [
        path.name,
        "reclaimerr-20260102-000000.db",
    ]
    assert _tables(path) == ["history", "rules"]
    assert not list(folder.glob("*.partial"))
    # the local staging copy is cleaned up too
    assert not list(source.parent.glob("*.partial"))


def test_unwritable_backup_folder_fails_with_a_clear_error(tmp_path: Path) -> None:
    blocked = tmp_path / "backups"
    blocked.write_text("a file where the folder should be")

    with pytest.raises(RuntimeError, match="not writable"):
        write_backup(4, blocked)


def test_stalled_backup_times_out_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def stalled(keep: int) -> Path:
        # stands in for a write blocked on an unresponsive network share
        release.wait(5)
        return tmp_path / "never.db"

    @asynccontextmanager
    async def untracked(task: object) -> AsyncGenerator[None, None]:
        yield

    async def retention() -> int:
        return 4

    monkeypatch.setattr(database_backup.settings, "backup_dir", tmp_path)
    monkeypatch.setattr(database_backup, "write_backup", stalled)
    monkeypatch.setattr(database_backup, "track_task_execution", untracked)
    monkeypatch.setattr(database_backup, "_backup_retention", retention)
    monkeypatch.setattr(database_backup, "BACKUP_TIMEOUT_SECONDS", 0.1)

    async def run() -> None:
        try:
            with pytest.raises(RuntimeError, match="did not finish"):
                await database_backup.backup_database()
        finally:
            release.set()

    asyncio.run(run())


@pytest.mark.parametrize("name", ["../reclaimerr.db", "notes.db", "reclaimerr.db"])
def test_only_backup_file_names_can_be_downloaded(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        resolve_backup(name, tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "/api/settings/backups",
        "/api/settings/backups/download",
        "/api/settings/backups/reclaimerr-20260101-000000.db",
    ],
)
def test_backup_routes_reject_non_admins(url: str) -> None:
    app = FastAPI()
    app.include_router(backup_routes.router, prefix="/api/settings")
    app.dependency_overrides[get_current_user] = lambda: User(
        username="viewer",
        password_hash="x",
        role=UserRole.USER,
        permissions=[],
    )

    response = TestClient(app).get(url)

    assert response.status_code == 403


def _make_restorable(path: Path, marker: str, version: str | None = None) -> None:
    if version is None:
        version = ScriptDirectory.from_config(alembic_config()).get_current_head()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    conn.execute("INSERT INTO alembic_version VALUES (?)", (version,))
    conn.execute("CREATE TABLE marker (value TEXT)")
    conn.execute("INSERT INTO marker VALUES (?)", (marker,))
    conn.commit()
    conn.close()


def _marker(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(database_backup.settings, "data_dir", tmp_path)
    return tmp_path


def test_staged_restore_replaces_the_database_and_keeps_the_old_one(
    data_dir: Path,
) -> None:
    live = database_backup.settings.db_path
    _make_restorable(live, "live")
    backup = data_dir / "reclaimerr-20260101-000000.db"
    _make_restorable(backup, "backup")

    stage_restore(lambda partial: partial.write_bytes(backup.read_bytes()))
    assert has_pending_restore()
    assert _marker(live) == "live"  # staging never touches the live database

    assert apply_pending_restore() is True

    assert _marker(live) == "backup"
    assert _marker(live.with_name("reclaimerr.db.pre-restore")) == "live"
    assert not has_pending_restore()


def test_cancelled_restore_is_not_applied(data_dir: Path) -> None:
    live = database_backup.settings.db_path
    _make_restorable(live, "live")
    backup = data_dir / "backup.db"
    _make_restorable(backup, "backup")
    stage_restore(lambda partial: partial.write_bytes(backup.read_bytes()))

    cancel_pending_restore()

    assert apply_pending_restore() is False
    assert _marker(live) == "live"


@pytest.mark.parametrize("kind", ["garbage", "not_reclaimerr", "newer"])
def test_invalid_backups_are_refused_and_never_staged(
    data_dir: Path, kind: str
) -> None:
    def write(partial: Path) -> None:
        if kind == "garbage":
            partial.write_bytes(b"this is not a database" * 100)
        elif kind == "not_reclaimerr":
            _make_source(partial)
        else:
            _make_restorable(partial, "x", version="ffffffffffff")

    with pytest.raises(InvalidBackupError):
        stage_restore(write)

    assert not has_pending_restore()
    assert not list(database_backup.settings.db_path.parent.glob("*.partial*"))


def test_bad_pending_file_leaves_the_live_database_alone(data_dir: Path) -> None:
    live = database_backup.settings.db_path
    _make_restorable(live, "live")
    pending_restore_path().write_bytes(b"corrupt")

    assert apply_pending_restore() is False

    assert _marker(live) == "live"
    assert not has_pending_restore()


def test_restore_routes_reject_non_admins() -> None:
    app = FastAPI()
    app.include_router(backup_routes.router, prefix="/api/settings")
    app.dependency_overrides[get_current_user] = lambda: User(
        username="viewer", password_hash="x", role=UserRole.USER, permissions=[]
    )
    client = TestClient(app)

    assert client.delete("/api/settings/backups/restore").status_code == 403
    assert (
        client.post(
            "/api/settings/backups/reclaimerr-20260101-000000.db/restore"
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/settings/backups/restore", files={"file": ("a.db", b"x")}
        ).status_code
        == 403
    )
