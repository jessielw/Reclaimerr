"""SQLite backups of the Reclaimerr database.

Reclaimerr is the tool that deletes things, so its own rules, protections and
history are worth keeping a copy of. Copies are made with SQLite's online backup
API, which is safe while the app is writing and folds in the WAL; copying the
file directly could capture a torn page or miss committed writes still in the
WAL.

Restore is deliberately manual: stop the app, replace ``reclaimerr.db`` and
start it again. Migrations run on start, so an older backup upgrades itself.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from backend.core.logger import LOG
from backend.core.settings import settings
from backend.core.task_tracking import track_task_execution
from backend.database import async_db
from backend.database.models import GeneralSettings
from backend.enums import Task

BACKUP_NAME_PATTERN = re.compile(r"^reclaimerr-\d{8}-\d{6}\.db$")
DEFAULT_BACKUP_RETENTION = 4


@dataclass(frozen=True, slots=True)
class BackupFile:
    name: str
    size_bytes: int
    created_at: datetime


def backups_dir() -> Path:
    return settings.data_dir_path / "backups"


def backup_file_name(now: datetime | None = None) -> str:
    return f"reclaimerr-{(now or datetime.now(UTC)):%Y%m%d-%H%M%S}.db"


def copy_database(destination: Path, source: Path | None = None) -> None:
    """Write a consistent copy of the live database to ``destination``."""
    source_conn = sqlite3.connect(source or settings.db_path)
    try:
        dest_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()


def list_backups(directory: Path | None = None) -> list[BackupFile]:
    """Return the backups in the folder, newest first."""
    folder = directory or backups_dir()
    if not folder.is_dir():
        return []
    files: list[BackupFile] = []
    for path in folder.iterdir():
        if not path.is_file() or not BACKUP_NAME_PATTERN.match(path.name):
            continue
        stat = path.stat()
        files.append(
            BackupFile(
                name=path.name,
                size_bytes=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            )
        )
    # the timestamped names sort chronologically, unlike mtimes after a copy
    files.sort(key=lambda item: item.name, reverse=True)
    return files


def resolve_backup(name: str, directory: Path | None = None) -> Path:
    """Return the path of one stored backup, refusing anything but a backup name."""
    if not BACKUP_NAME_PATTERN.match(name):
        raise ValueError("Not a backup file name")
    path = (directory or backups_dir()) / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def prune_backups(keep: int, directory: Path | None = None) -> list[str]:
    """Delete all but the newest ``keep`` backups. Other files are never touched."""
    stale = list_backups(directory)[max(keep, 1) :]
    folder = directory or backups_dir()
    for item in stale:
        (folder / item.name).unlink(missing_ok=True)
    return [item.name for item in stale]


def write_backup(keep: int, directory: Path | None = None) -> Path:
    """Write a new timestamped backup into the folder, then apply retention."""
    folder = directory or backups_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Backup folder {folder} is not writable: {exc}") from exc

    target = folder / backup_file_name()
    # written under a name retention ignores, so a crash mid-copy never leaves
    # a broken file that looks like a good backup
    partial = target.with_name(f"{target.name}.partial")
    try:
        copy_database(partial)
        partial.replace(target)
    except (OSError, sqlite3.Error) as exc:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Could not write backup to {folder}: {exc}") from exc

    removed = prune_backups(keep, folder)
    if removed:
        LOG.info(f"Removed {len(removed)} old database backup(s)")
    return target


async def _backup_retention() -> int:
    async with async_db() as session:
        retention = await session.scalar(
            select(GeneralSettings.database_backup_retention)
        )
    return int(retention or DEFAULT_BACKUP_RETENTION)


async def backup_database() -> None:
    """Scheduled task: write a backup into the data folder and keep the newest N."""
    async with track_task_execution(Task.BACKUP_DATABASE):
        keep = await _backup_retention()
        path = await asyncio.to_thread(write_backup, keep)
        LOG.info(f"Database backed up to {path}")
