import logging
import os
import queue
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from typing_extensions import override

from backend.core.__version__ import __version__, program_name
from backend.core.settings import settings
from backend.core.task_env import TASK_CHILD_ENV
from backend.enums import LogLevel, LogSource

_LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

# An isolated task child's lines land in the parent's log exactly as the child
# wrote them, so they carry a marker saying which process they came from.
_TASK_CHILD_LOG_FORMAT = "%(asctime)s - %(levelname)s - [task-child] %(message)s"

# Level names as they appear in an already formatted line. Used to keep a line
# forwarded from a task child at the severity the child gave it.
_FORWARDED_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class _RawAwareFormatter(logging.Formatter):
    """Formatter that leaves already formatted records untouched.

    Lines forwarded from an isolated task child arrive with the child's own
    timestamp and level already applied, so formatting them a second time would
    stamp every line twice.
    """

    @override
    def format(self, record: logging.LogRecord) -> str:
        if getattr(record, "raw", False):
            return record.getMessage()
        return super().format(record)


class Logger:
    SRC = LogSource
    LVL = LogLevel

    def __init__(
        self,
        log_file: Path,
        log_level: LogLevel = LogLevel.DEBUG,
        to_console: bool = False,
        to_file: bool = True,
        default_source: LogSource = LogSource.BE,
        log_format: str = _LOG_FORMAT,
    ) -> None:
        self.logger = logging.getLogger("reclaimerr")
        self.logger.setLevel(log_level.value)
        self.log_file = log_file
        self.log_level = log_level
        self.to_console = to_console
        self.to_file = to_file
        self.default_source = default_source
        self.log_format = log_format
        self._queue_listener: QueueListener | None = None
        self._initialized = False

        if to_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)

    def _initialize_handlers(self) -> None:
        if self._initialized:  # already initialized, nothing to do
            return
        self._initialized = True

        fmt = _RawAwareFormatter(self.log_format)
        sinks: list[logging.Handler] = []

        if self.to_file:
            file_handler = TimedRotatingFileHandler(
                self.log_file,
                when="midnight",
                backupCount=settings.log_retention_days,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            sinks.append(file_handler)

        if self.to_console:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(fmt)
            sinks.append(console_handler)

        # QueueListener runs the sinks on a background thread, keeping I/O off the event loop
        log_queue: queue.Queue[Any] = queue.Queue(maxsize=-1)
        self._queue_listener = QueueListener(
            log_queue, *sinks, respect_handler_level=True
        )
        self._queue_listener.start()

        # attach a QueueHandler so all logger.* calls enqueue (non blocking)
        self.logger.addHandler(QueueHandler(log_queue))

        # log initial program info on first initialization
        self.info(f"{program_name} v{__version__}")

    def debug(
        self, message: str, source: LogSource | None = None, exc_info: bool = False
    ) -> None:
        if self.logger.level <= logging.DEBUG:
            self._initialize_handlers()
            src = source or self.default_source
            self.logger.debug(f"{src.value}: {str(message).strip()}", exc_info=exc_info)

    def info(
        self, message: str, source: LogSource | None = None, exc_info: bool = False
    ) -> None:
        if self.logger.level <= logging.INFO:
            self._initialize_handlers()
            src = source or self.default_source
            self.logger.info(f"{src.value}: {str(message).strip()}", exc_info=exc_info)

    def warning(
        self, message: str, source: LogSource | None = None, exc_info: bool = False
    ) -> None:
        if self.logger.level <= logging.WARNING:
            self._initialize_handlers()
            src = source or self.default_source
            self.logger.warning(
                f"{src.value}: {str(message).strip()}", exc_info=exc_info
            )

    def error(
        self, message: str, source: LogSource | None = None, exc_info: bool = False
    ) -> None:
        if self.logger.level <= logging.ERROR:
            self._initialize_handlers()
            src = source or self.default_source
            self.logger.error(f"{src.value}: {str(message).strip()}", exc_info=exc_info)

    def critical(
        self, message: str, source: LogSource | None = None, exc_info: bool = False
    ) -> None:
        if self.logger.level <= logging.CRITICAL:
            self._initialize_handlers()
            src = source or self.default_source
            self.logger.critical(
                f"{src.value}: {str(message).strip()}", exc_info=exc_info
            )

    def exception(self, message: str, source: LogSource | None = None) -> None:
        """Log exception with traceback (use within except block)"""
        if self.logger.level <= logging.ERROR:
            self._initialize_handlers()
            src = source or self.default_source
            self.logger.exception(f"{src.value}: {str(message).strip()}")

    def forward(self, line: str) -> None:
        """Re-emit a line an isolated task child already formatted for itself.

        The child streams its output to the parent instead of opening the log
        file a second time, so these lines arrive complete. Routing them back
        through ``info()`` would add another timestamp and source tag to each
        one, so they are emitted verbatim at the level the child used.
        """
        parts = line.split(" - ", 2)
        level = (
            _FORWARDED_LEVELS.get(parts[1], logging.INFO)
            if len(parts) == 3
            else logging.INFO
        )
        if self.logger.level > level:
            return
        self._initialize_handlers()
        self.logger.log(level, line, extra={"raw": True})

    def set_log_level(self, log_level: LogLevel) -> None:
        self.logger.setLevel(log_level.value)

    def stop(self) -> None:
        """Stop the queue listener (flush and join the background thread). Call on app shutdown."""
        if self._queue_listener is not None:
            self._queue_listener.stop()
            self._queue_listener = None


# initialize global logger instance with a static filename
_log_filename = f"{program_name.lower().replace(' ', '_')}.log"
_log_path = settings.log_dir / _log_filename
_is_task_child = os.getenv(TASK_CHILD_ENV) == "1"
LOG = Logger(
    _log_path,
    to_console=True,
    # An isolated task child streams everything to its parent on stderr and the
    # parent writes it to this same file, so a child opening the file as well
    # would log every line twice (and fight the parent over midnight rotation).
    to_file=not _is_task_child,
    log_format=_TASK_CHILD_LOG_FORMAT if _is_task_child else _LOG_FORMAT,
)
