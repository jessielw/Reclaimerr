from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.api.routes.settings import logs as logs_route
from backend.database.models import User
from backend.enums import UserRole
from backend.services import log_reader

BASE_NAME = "reclaimerr.log"

SAMPLE = "\n".join(
    [
        "2026-09-20 10:00:00,001 - INFO - [BE]: Sync Media started",
        "2026-09-20 10:00:01,002 - DEBUG - [BE]: Looked at 12 items",
        "2026-09-20 10:00:02,003 - WARNING - [FE]: Slow response",
        "2026-09-20 10:00:03,004 - ERROR - [BE]: Radarr delete failed",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ValueError: nope",
        "2026-09-20 10:00:04,005 - INFO - [task-child] [BE]: Scan finished",
    ]
)


def _admin() -> User:
    return User(username="admin", password_hash="x", role=UserRole.ADMIN)


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the reader at a throwaway directory holding a fake log."""
    (tmp_path / BASE_NAME).write_text(SAMPLE, encoding="utf-8")
    (tmp_path / f"{BASE_NAME}.2026-09-19").write_text(
        "2026-09-19 08:00:00,000 - INFO - [BE]: Yesterday\n", encoding="utf-8"
    )
    (tmp_path / "secrets.env").write_text("TOKEN=hunter2\n", encoding="utf-8")

    monkeypatch.setattr(log_reader, "_log_dir", lambda: tmp_path.resolve())
    monkeypatch.setattr(log_reader, "_base_name", lambda: BASE_NAME)
    return tmp_path


def test_entries_come_back_oldest_first(log_dir: Path) -> None:
    tail = log_reader.tail_log(log_dir / BASE_NAME)

    assert [entry.level for entry in tail.entries] == [
        "INFO",
        "DEBUG",
        "WARNING",
        "ERROR",
        "INFO",
    ]
    assert tail.entries[0].message == "Sync Media started"
    assert tail.scan_truncated is False


def test_the_limit_keeps_the_newest_entries(log_dir: Path) -> None:
    tail = log_reader.tail_log(log_dir / BASE_NAME, limit=2)

    assert len(tail.entries) == 2
    assert tail.entries[-1].message == "Scan finished"


def test_a_traceback_is_attached_to_the_entry_it_belongs_to(log_dir: Path) -> None:
    tail = log_reader.tail_log(log_dir / BASE_NAME)

    error = next(entry for entry in tail.entries if entry.level == "ERROR")
    assert "Radarr delete failed" in error.message
    assert "ValueError: nope" in error.message
    # the traceback must not become entries of its own
    assert len(tail.entries) == 5


def test_the_level_filter_keeps_that_level_and_above(log_dir: Path) -> None:
    tail = log_reader.tail_log(log_dir / BASE_NAME, min_level="WARNING")

    assert [entry.level for entry in tail.entries] == ["WARNING", "ERROR"]


def test_the_search_filter_matches_anywhere_in_the_entry(log_dir: Path) -> None:
    tail = log_reader.tail_log(log_dir / BASE_NAME, search="radarr")

    assert len(tail.entries) == 1
    assert tail.entries[0].level == "ERROR"


def test_the_search_filter_reaches_into_an_attached_traceback(log_dir: Path) -> None:
    tail = log_reader.tail_log(log_dir / BASE_NAME, search="ValueError")

    assert len(tail.entries) == 1
    assert tail.entries[0].level == "ERROR"


def test_source_and_task_child_markers_are_split_out(log_dir: Path) -> None:
    tail = log_reader.tail_log(log_dir / BASE_NAME)

    frontend = next(entry for entry in tail.entries if entry.level == "WARNING")
    assert frontend.source == "[FE]"
    assert frontend.task_child is False

    child = tail.entries[-1]
    assert child.task_child is True
    assert child.source == "[BE]"
    assert child.message == "Scan finished"


def test_a_short_window_reports_that_it_did_not_reach_the_start(
    log_dir: Path,
) -> None:
    tail = log_reader.tail_log(log_dir / BASE_NAME, scan_bytes=120)

    assert tail.scan_truncated is True
    assert tail.file_size_bytes == (log_dir / BASE_NAME).stat().st_size
    # only the tail of the file was read, so the opening entry is not in it
    assert all("Sync Media started" not in entry.raw for entry in tail.entries)


def test_a_rotated_file_resolves(log_dir: Path) -> None:
    resolved = log_reader.resolve_log_file(f"{BASE_NAME}.2026-09-19")

    assert resolved == (log_dir / f"{BASE_NAME}.2026-09-19").resolve()


@pytest.mark.parametrize(
    "name",
    [
        "../secrets.env",
        "..\\secrets.env",
        "subdir/reclaimerr.log",
        "..",
        # inside the log directory, but not one of our own files
        "secrets.env",
    ],
)
def test_a_name_that_escapes_or_does_not_belong_is_refused(
    log_dir: Path, name: str
) -> None:
    with pytest.raises(ValueError):
        log_reader.resolve_log_file(name)


def test_an_absolute_path_is_refused(log_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        log_reader.resolve_log_file(str(tmp_path / "secrets.env"))


def test_a_missing_log_file_is_reported_as_missing(log_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        log_reader.resolve_log_file(f"{BASE_NAME}.2020-01-01")


def test_the_listing_puts_the_live_file_first(log_dir: Path) -> None:
    files = log_reader.list_log_files()

    assert [item.name for item in files] == [
        BASE_NAME,
        f"{BASE_NAME}.2026-09-19",
    ]
    assert files[0].is_current is True
    assert files[1].is_current is False


@pytest.mark.anyio
async def test_the_endpoint_returns_parsed_entries(log_dir: Path) -> None:
    response = await logs_route.get_logs(_admin=_admin(), level="error")

    assert response.file == BASE_NAME
    assert len(response.entries) == 1
    assert response.entries[0].level == "ERROR"
    assert response.configured_level


@pytest.mark.anyio
async def test_the_endpoint_rejects_an_unknown_level(log_dir: Path) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await logs_route.get_logs(_admin=_admin(), level="LOUD")

    assert excinfo.value.status_code == 400


@pytest.mark.anyio
async def test_the_endpoint_rejects_a_traversing_file_name(log_dir: Path) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await logs_route.get_logs(_admin=_admin(), file="../secrets.env")

    assert excinfo.value.status_code == 400


@pytest.mark.anyio
async def test_the_endpoint_404s_on_a_missing_file(log_dir: Path) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await logs_route.get_logs(_admin=_admin(), file=f"{BASE_NAME}.2020-01-01")

    assert excinfo.value.status_code == 404


@pytest.mark.anyio
async def test_the_download_endpoint_refuses_to_leave_the_log_directory(
    log_dir: Path,
) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await logs_route.download_log(_admin=_admin(), file="../secrets.env")

    assert excinfo.value.status_code == 400
