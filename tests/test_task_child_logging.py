from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from backend.core.logger import (
    _TASK_CHILD_LOG_FORMAT,
    Logger,
    _RawAwareFormatter,
)
from backend.enums import LogLevel

_CHILD_LINE = "2026-09-21 09:00:15,640 - WARNING - [BE]: Plex service cleared"


class _StubLogger:
    """Stands in for the shared ``reclaimerr`` logger so records stay local."""

    def __init__(self, level: int = logging.INFO) -> None:
        self.level = level
        self.records: list[tuple[int, str, dict[str, Any] | None]] = []

    def log(
        self, level: int, message: str, extra: dict[str, Any] | None = None
    ) -> None:
        self.records.append((level, message, extra))


@pytest.fixture
def child_logger(tmp_path: Path):
    """A logger wired to a stub so nothing reaches the real sinks."""
    shared = logging.getLogger("reclaimerr")
    previous_level = shared.level
    log = Logger(tmp_path / "logs" / "reclaimerr.log", to_console=False, to_file=False)
    log._initialized = True  # no sinks needed, the stub captures everything
    log.logger = _StubLogger()  # pyright: ignore[reportAttributeAccessIssue]
    yield log
    shared.setLevel(previous_level)


def test_task_child_logger_never_opens_the_shared_log_file(tmp_path: Path) -> None:
    """The parent owns the log file - a child writing to it too would double every line."""
    log_file = tmp_path / "logs" / "reclaimerr.log"

    Logger(log_file, to_console=True, to_file=False)

    assert not log_file.parent.exists()


def test_forwarded_line_keeps_the_level_the_child_gave_it(child_logger) -> None:
    child_logger.forward(_CHILD_LINE)

    assert child_logger.logger.records == [
        (logging.WARNING, _CHILD_LINE, {"raw": True})
    ]


def test_forwarded_line_without_a_level_falls_back_to_info(child_logger) -> None:
    child_logger.forward('  File "backend\\core\\task_child.py", line 41')

    level, message, _extra = child_logger.logger.records[0]
    assert level == logging.INFO
    assert message == '  File "backend\\core\\task_child.py", line 41'


def test_forwarded_line_below_the_configured_level_is_dropped(child_logger) -> None:
    child_logger.logger.level = LogLevel.ERROR.value

    child_logger.forward(_CHILD_LINE)

    assert child_logger.logger.records == []


def test_raw_records_are_not_formatted_a_second_time() -> None:
    formatter = _RawAwareFormatter("%(asctime)s - %(levelname)s - %(message)s")
    raw = logging.LogRecord(
        "reclaimerr", logging.WARNING, __file__, 1, _CHILD_LINE, None, None
    )
    raw.raw = True  # pyright: ignore[reportAttributeAccessIssue]
    normal = logging.LogRecord(
        "reclaimerr", logging.WARNING, __file__, 1, "[BE]: hello", None, None
    )

    assert formatter.format(raw) == _CHILD_LINE
    assert formatter.format(normal).endswith(" - WARNING - [BE]: hello")


def test_task_child_lines_say_which_process_they_came_from() -> None:
    """Forwarded lines are verbatim, so the child has to mark them itself."""
    formatter = _RawAwareFormatter(_TASK_CHILD_LOG_FORMAT)
    record = logging.LogRecord(
        "reclaimerr", logging.INFO, __file__, 1, "[BE]: Task started", None, None
    )

    assert formatter.format(record).endswith(
        " - INFO - [task-child] [BE]: Task started"
    )


def test_the_child_marker_does_not_hide_the_level_from_forward(child_logger) -> None:
    child_logger.forward("2026-09-21 09:19:56,761 - ERROR - [task-child] [BE]: boom")

    level, _message, _extra = child_logger.logger.records[0]
    assert level == logging.ERROR
