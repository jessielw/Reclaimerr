"""SQLite backups of the Reclaimerr database.

Reclaimerr is the tool that deletes things, so its own rules, protections and
history are worth keeping a copy of. Copies are made with SQLite's online backup
API, which is safe while the app is writing and folds in the WAL; copying the
file directly could capture a torn page or miss committed writes still in the
WAL.

Restore is staged, never live: the app validates a backup and parks it next to
the database, and the swap happens on the next start before any connection is
opened. Migrations run after that, so an older backup upgrades itself. Replacing
``reclaimerr.db`` by hand with the app stopped works too.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util.exc import CommandError
from sqlalchemy import select

from backend.core.logger import LOG
from backend.core.settings import settings
from backend.core.task_tracking import track_task_execution
from backend.database import alembic_config, async_db
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
    return settings.backup_dir or settings.data_dir_path / "backups"


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


class InvalidBackupError(ValueError):
    """The file is not a backup this version of Reclaimerr can restore."""


def pending_restore_path() -> Path:
    # next to the live database so applying it is an atomic rename on one disk
    return settings.db_path.with_name("restore.pending.db")


def has_pending_restore() -> bool:
    return pending_restore_path().is_file()


def validate_backup(path: Path) -> None:
    """Raise InvalidBackupError unless ``path`` is an intact, restorable database."""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise InvalidBackupError(
            f"Could not open the file as a database: {exc}"
        ) from exc
    try:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise InvalidBackupError("The backup failed its integrity check")
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error as exc:
        raise InvalidBackupError("Not a Reclaimerr database") from exc
    finally:
        conn.close()

    if row is None:
        raise InvalidBackupError("Not a Reclaimerr database")
    try:
        ScriptDirectory.from_config(alembic_config()).get_revision(row[0])
    except (RevisionError, CommandError) as exc:
        raise InvalidBackupError(
            "This backup was made by a newer version of Reclaimerr"
        ) from exc


def stage_restore(write_source: Callable[[Path], None]) -> None:
    """Validate a backup and stage it to be restored on the next start.

    The live database is never touched here: a running app holds open
    connections, so the swap happens in ``apply_pending_restore`` at startup.
    """
    pending = pending_restore_path()
    partial = pending.with_name(f"{pending.name}.partial")
    try:
        write_source(partial)
        validate_backup(partial)
        partial.replace(pending)
    except InvalidBackupError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Could not stage the restore: {exc}") from exc
    finally:
        partial.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{partial}{suffix}").unlink(missing_ok=True)


def stage_stored_backup(name: str) -> None:
    source = resolve_backup(name)

    def write_source(partial: Path) -> None:
        shutil.copyfile(source, partial)

    stage_restore(write_source)


def cancel_pending_restore() -> None:
    pending_restore_path().unlink(missing_ok=True)


def apply_pending_restore() -> bool:
    """Swap a staged backup in as the live database. Call before the engine opens.

    The database being replaced is kept as ``reclaimerr.db.pre-restore`` so a
    wrong restore can be undone. On any failure the live database is left as is.
    """
    pending = pending_restore_path()
    if not pending.is_file():
        return False

    live = settings.db_path
    kept = live.with_name(f"{live.name}.pre-restore")
    try:
        validate_backup(pending)
        if live.exists():
            # backup API rather than a rename, so rows still in the WAL are kept
            kept.unlink(missing_ok=True)
            copy_database(kept, source=live)
        # a stale WAL next to the new file would be replayed over it
        for suffix in ("-wal", "-shm"):
            Path(f"{live}{suffix}").unlink(missing_ok=True)
            Path(f"{pending}{suffix}").unlink(missing_ok=True)
        pending.replace(live)
    except (OSError, sqlite3.Error, InvalidBackupError) as exc:
        LOG.error(f"Could not restore the staged database backup: {exc}")
        pending.unlink(missing_ok=True)
        return False

    LOG.info(f"Restored the database from a staged backup; previous kept as {kept}")
    return True


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
