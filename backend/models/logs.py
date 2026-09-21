from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LogFileInfo(BaseModel):
    name: str
    size_bytes: int
    modified_at: datetime
    is_current: bool


class LogEntryItem(BaseModel):
    """One logged event.

    ``timestamp`` and ``level`` are null for a continuation line that had no
    header of its own, which happens when the read window opened mid-entry.
    """

    raw: str
    message: str
    timestamp: str | None = None
    level: str | None = None
    source: str | None = None
    task_child: bool = False


class LogTailResponse(BaseModel):
    file: str
    entries: list[LogEntryItem]
    file_size_bytes: int
    scan_truncated: bool
    configured_level: str
