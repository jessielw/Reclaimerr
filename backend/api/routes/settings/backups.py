from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from backend.core.auth import require_admin
from backend.database.models import User
from backend.models.settings import DatabaseBackupInfo
from backend.services.database_backup import (
    backup_file_name,
    copy_database,
    list_backups,
    resolve_backup,
)

router = APIRouter(tags=["settings", "backups"])

# The database holds service API keys, media-server tokens and SMTP settings,
# so every route here is admin only.


@router.get("/backups", response_model=list[DatabaseBackupInfo])
async def get_backups(
    _admin: Annotated[User, Depends(require_admin)],
) -> list[DatabaseBackupInfo]:
    """List the scheduled backups kept in the data folder, newest first."""
    files = await asyncio.to_thread(list_backups)
    return [
        DatabaseBackupInfo(
            name=item.name, size_bytes=item.size_bytes, created_at=item.created_at
        )
        for item in files
    ]


@router.get("/backups/download")
async def download_fresh_backup(
    _admin: Annotated[User, Depends(require_admin)],
) -> FileResponse:
    """Make a consistent copy of the live database right now and download it."""
    folder = Path(tempfile.mkdtemp(prefix="reclaimerr-backup-"))
    path = folder / backup_file_name()

    def cleanup() -> None:
        path.unlink(missing_ok=True)
        folder.rmdir()

    try:
        await asyncio.to_thread(copy_database, path)
    except Exception as exc:
        cleanup()
        raise HTTPException(
            status_code=500, detail=f"Could not back up the database: {exc}"
        ) from exc
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=path.name,
        background=BackgroundTask(cleanup),
    )


@router.get("/backups/{name}")
async def download_backup(
    name: str,
    _admin: Annotated[User, Depends(require_admin)],
) -> FileResponse:
    """Download one stored backup."""
    try:
        path = resolve_backup(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid backup name") from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup not found") from None
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=path.name)
