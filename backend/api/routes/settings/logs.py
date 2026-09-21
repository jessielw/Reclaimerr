from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from backend.core.auth import require_admin
from backend.core.settings import settings
from backend.database.models import User
from backend.enums import LogLevel
from backend.models.logs import LogEntryItem, LogFileInfo, LogTailResponse
from backend.services.log_reader import (
    list_log_files,
    resolve_log_file,
    tail_log,
)

router = APIRouter(tags=["settings", "logs"])

LOG_LEVEL_NAMES = tuple(level.name for level in LogLevel)


def _resolve_or_404(name: str | None):
    try:
        return resolve_log_file(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Log file not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/logs/files", response_model=list[LogFileInfo])
async def get_log_files(
    _admin: Annotated[User, Depends(require_admin)],
) -> list[LogFileInfo]:
    """List the current log file and the rotated files kept beside it."""
    files = await asyncio.to_thread(list_log_files)
    return [
        LogFileInfo(
            name=item.name,
            size_bytes=item.size_bytes,
            modified_at=item.modified_at,
            is_current=item.is_current,
        )
        for item in files
    ]


@router.get("/logs", response_model=LogTailResponse)
async def get_logs(
    _admin: Annotated[User, Depends(require_admin)],
    file: Annotated[
        str | None,
        Query(description="Log file name; defaults to the current log"),
    ] = None,
    lines: Annotated[int, Query(ge=1, le=5000)] = 500,
    level: Annotated[
        str | None,
        Query(description="Lowest level to include, e.g. WARNING"),
    ] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> LogTailResponse:
    """Return the most recent log entries, newest last.

    Filtering is applied across the whole window that is read, not only the
    ``lines`` that come back, so asking for errors still finds them when they
    are buried under thousands of debug lines.
    """
    if level is not None and level.upper() not in LOG_LEVEL_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"level must be one of {', '.join(LOG_LEVEL_NAMES)}",
        )

    path = _resolve_or_404(file)
    tail = await asyncio.to_thread(
        tail_log,
        path,
        limit=lines,
        min_level=level.upper() if level else None,
        search=(search or "").strip() or None,
    )

    return LogTailResponse(
        file=tail.file,
        entries=[
            LogEntryItem(
                raw=entry.raw,
                message=entry.message,
                timestamp=entry.timestamp,
                level=entry.level,
                source=entry.source,
                task_child=entry.task_child,
            )
            for entry in tail.entries
        ],
        file_size_bytes=tail.file_size_bytes,
        scan_truncated=tail.scan_truncated,
        configured_level=settings.log_level_enum.name,
    )


@router.get("/logs/download")
async def download_log(
    _admin: Annotated[User, Depends(require_admin)],
    file: Annotated[str | None, Query()] = None,
) -> FileResponse:
    """Download a log file as-is."""
    path = _resolve_or_404(file)
    return FileResponse(path, media_type="text/plain", filename=path.name)
