from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.settings import backups as backup_routes
from backend.core.auth import get_current_user
from backend.database.models import User
from backend.enums import UserRole
from backend.services import database_backup
from backend.services.database_backup import (
    copy_database,
    list_backups,
    prune_backups,
    resolve_backup,
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
    source = tmp_path / "reclaimerr.db"
    _make_source(source)
    folder = tmp_path / "backups"
    folder.mkdir()
    for day in (1, 2):
        (folder / f"reclaimerr-2026010{day}-000000.db").write_bytes(b"")
    monkeypatch.setattr(
        database_backup,
        "copy_database",
        lambda destination: copy_database(destination, source=source),
    )

    path = write_backup(2, folder)

    assert [item.name for item in list_backups(folder)] == [
        path.name,
        "reclaimerr-20260102-000000.db",
    ]
    assert _tables(path) == ["history", "rules"]
    assert not list(folder.glob("*.partial"))


def test_unwritable_backup_folder_fails_with_a_clear_error(tmp_path: Path) -> None:
    blocked = tmp_path / "backups"
    blocked.write_text("a file where the folder should be")

    with pytest.raises(RuntimeError, match="not writable"):
        write_backup(4, blocked)


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
