from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from backend.core.logger import LOG
from backend.core.settings import settings
from backend.enums import LogLevel, LogSource

__all__ = [
    "LogEntry",
    "LogFile",
    "LogTail",
    "list_log_files",
    "resolve_log_file",
    "tail_log",
]

# The file handler writes "%(asctime)s - %(levelname)s - %(message)s", and the
# message itself is prefixed with the task-child marker and the source tag.
_LINE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) - "
    r"(?P<rest>.*)$"
)
_TASK_CHILD_MARKER = "[task-child] "
_SOURCE_PREFIXES = {source.value: source for source in LogSource}

_LEVEL_ORDER = {level.name: level.value for level in LogLevel}

# How much of a file a single request will read. Bounded so a multi-gigabyte
# debug log cannot be pulled into memory, and so the answer stays fast.
DEFAULT_SCAN_BYTES = 4 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024


@dataclass(slots=True)
class LogEntry:
    """One logged event, including any continuation lines such as a traceback."""

    raw: str
    message: str
    timestamp: str | None = None
    level: str | None = None
    source: str | None = None
    task_child: bool = False


@dataclass(slots=True)
class LogFile:
    name: str
    size_bytes: int
    modified_at: datetime
    is_current: bool


@dataclass(slots=True)
class LogTail:
    file: str
    entries: list[LogEntry] = field(default_factory=list)
    file_size_bytes: int = 0
    # True when the window read did not reach the start of the file, so older
    # entries exist that this response never looked at
    scan_truncated: bool = False


def _log_dir() -> Path:
    return settings.log_dir.resolve()


def _base_name() -> str:
    return LOG.log_file.name


def list_log_files() -> list[LogFile]:
    """Return the current log plus the rotated files beside it, newest first."""
    base = _base_name()
    directory = _log_dir()
    files: list[LogFile] = []
    for path in directory.glob(f"{base}*"):
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            LogFile(
                name=path.name,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                is_current=path.name == base,
            )
        )
    # the live file first, then rotated files newest to oldest
    files.sort(key=lambda item: (not item.is_current, -item.modified_at.timestamp()))
    return files


def resolve_log_file(name: str | None) -> Path:
    """Resolve a requested log file name to a path inside the log directory.

    The name comes straight from a query string, so it is treated as hostile:
    anything with a path separator, anything that escapes the log directory
    after resolution, and anything that is not one of this app's own log files
    is refused rather than read.
    """
    directory = _log_dir()
    base = _base_name()
    if not name:
        return (directory / base).resolve()

    candidate_name = name.strip()
    if (
        not candidate_name
        or candidate_name in {".", ".."}
        or "/" in candidate_name
        or "\\" in candidate_name
        or Path(candidate_name).name != candidate_name
    ):
        raise ValueError("Invalid log file name")

    resolved = (directory / candidate_name).resolve()
    if not resolved.is_relative_to(directory):
        raise ValueError("Invalid log file name")
    if not resolved.name.startswith(base):
        raise ValueError("Invalid log file name")
    if not resolved.is_file():
        raise FileNotFoundError(candidate_name)
    return resolved


def _parse_line(line: str) -> LogEntry | None:
    """Turn one formatted line into an entry, or None if it is a continuation."""
    match = _LINE.match(line)
    if match is None:
        return None

    rest = match.group("rest")
    task_child = rest.startswith(_TASK_CHILD_MARKER)
    if task_child:
        rest = rest[len(_TASK_CHILD_MARKER) :]

    source: str | None = None
    for prefix in _SOURCE_PREFIXES:
        if rest.startswith(f"{prefix}: "):
            source = prefix
            rest = rest[len(prefix) + 2 :]
            break

    return LogEntry(
        raw=line,
        message=rest,
        timestamp=match.group("timestamp"),
        level=match.group("level"),
        source=source,
        task_child=task_child,
    )


def _group(lines: list[str]) -> list[LogEntry]:
    """Attach unparsable lines (tracebacks, bare output) to the entry above."""
    entries: list[LogEntry] = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed is not None:
            entries.append(parsed)
        elif entries:
            entries[-1].raw = f"{entries[-1].raw}\n{line}"
            entries[-1].message = f"{entries[-1].message}\n{line}"
        else:
            # the window opened mid-entry, so this has no header to belong to
            entries.append(LogEntry(raw=line, message=line))
    return entries


def _passes(entry: LogEntry, min_level: str | None, search: str | None) -> bool:
    if min_level is not None:
        threshold = _LEVEL_ORDER.get(min_level, 0)
        # an entry with no level is a stray line; keep it only at the floor
        value = _LEVEL_ORDER.get(entry.level or "", 0)
        if value < threshold:
            return False
    if search:
        return search.lower() in entry.raw.lower()
    return True


def tail_log(
    path: Path,
    *,
    limit: int = 500,
    min_level: str | None = None,
    search: str | None = None,
    scan_bytes: int = DEFAULT_SCAN_BYTES,
) -> LogTail:
    """Read the tail of a log file and return its most recent entries.

    Only the last ``scan_bytes`` of the file are read, so this is bounded no
    matter how large the log has grown. Filtering happens across everything in
    that window, which is why the window is much larger than ``limit``.
    """
    scan_bytes = max(1, min(scan_bytes, MAX_SCAN_BYTES))
    size = path.stat().st_size
    start = max(0, size - scan_bytes)

    with path.open("rb") as handle:
        handle.seek(start)
        blob = handle.read()

    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    scan_truncated = start > 0
    if scan_truncated and lines:
        # the first line was cut in half by the seek
        lines = lines[1:]

    entries = [entry for entry in _group(lines) if _passes(entry, min_level, search)]

    return LogTail(
        file=path.name,
        entries=entries[-limit:],
        file_size_bytes=size,
        scan_truncated=scan_truncated,
    )
