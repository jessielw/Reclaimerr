from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from backend.core.auth import require_admin
from backend.database.models import User
from backend.models.settings import DatabaseBackupInfo, DatabaseBackupList
from backend.services.database_backup import (
    InvalidBackupError,
    backup_file_name,
    backup_media_type,
    backups_dir,
    cancel_pending_restore,
    create_backup_archive,
    has_pending_restore,
    list_backups,
    resolve_backup,
    stage_restore,
    stage_stored_backup,
)

router = APIRouter(tags=["settings", "backups"])

# The database holds service API keys, media-server tokens and SMTP settings,
# so every route here is admin only.


@router.get("/backups", response_model=DatabaseBackupList)
async def get_backups(
    _admin: Annotated[User, Depends(require_admin)],
) -> DatabaseBackupList:
    """List the scheduled backups and the folder they are kept in, newest first."""
    files = await asyncio.to_thread(list_backups)
    return DatabaseBackupList(
        directory=str(backups_dir()),
        restore_pending=has_pending_restore(),
        backups=[
            DatabaseBackupInfo(
                name=item.name, size_bytes=item.size_bytes, created_at=item.created_at
            )
            for item in files
        ],
    )


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
        await asyncio.to_thread(create_backup_archive, path)
    except Exception as exc:
        cleanup()
        raise HTTPException(
            status_code=500, detail=f"Could not back up the database: {exc}"
        ) from exc
    return FileResponse(
        path,
        media_type=backup_media_type(path),
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
    return FileResponse(path, media_type=backup_media_type(path), filename=path.name)


# Restores are staged, not applied: the swap happens on the next start, before
# the database is opened (see ``apply_pending_restore``).


async def _stage(write_source) -> None:
    try:
        await asyncio.to_thread(stage_restore, write_source)
    except InvalidBackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/backups/restore", status_code=202)
async def stage_uploaded_restore(
    _admin: Annotated[User, Depends(require_admin)],
    file: UploadFile = File(...),
) -> dict[str, bool]:
    """Stage an uploaded backup to be restored on the next start."""

    def write_source(partial: Path) -> None:
        with partial.open("wb") as out:
            shutil.copyfileobj(file.file, out)

    await _stage(write_source)
    return {"restore_pending": True}


@router.post("/backups/{name}/restore", status_code=202)
async def stage_stored_restore(
    name: str,
    _admin: Annotated[User, Depends(require_admin)],
) -> dict[str, bool]:
    """Stage one stored backup to be restored on the next start."""
    try:
        await asyncio.to_thread(resolve_backup, name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid backup name") from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup not found") from None
    try:
        await asyncio.to_thread(stage_stored_backup, name)
    except InvalidBackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"restore_pending": True}


@router.delete("/backups/restore")
async def cancel_restore(
    _admin: Annotated[User, Depends(require_admin)],
) -> dict[str, bool]:
    """Cancel a staged restore before it is applied."""
    await asyncio.to_thread(cancel_pending_restore)
    return {"restore_pending": False}
