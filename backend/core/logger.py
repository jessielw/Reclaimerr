import logging
import queue
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from pathlib import Path

from backend.core.__version__ import __version__, program_name
from backend.core.settings import settings
from backend.enums import LogLevel, LogSource


class Logger:
    SRC = LogSource
    LVL = LogLevel

    def __init__(
        self,
        log_file: Path,
        log_level: LogLevel = LogLevel.DEBUG,
        to_console: bool = False,
        default_source: LogSource = LogSource.BE,
    ) -> None:
        self.logger = logging.getLogger("reclaimerr")
        self.logger.setLevel(log_level.value)
        self.log_file = log_file
        self.log_level = log_level
        self.to_console = to_console
        self.default_source = default_source
        self._queue_listener: QueueListener | None = None
        self._initialized = False

        log_file.parent.mkdir(parents=True, exist_ok=True)

    def _initialize_file_handler(self) -> None:
        if self._initialized:  # already initialized, nothing to do
            return
        self._initialized = True

        fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        file_handler = TimedRotatingFileHandler(
            self.log_file,
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        sinks: list[logging.Handler] = [file_handler]

        if self.to_console:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(fmt)
            sinks.append(console_handler)

        # QueueListener runs the sinks on a background thread, keeping I/O off the event loop
        log_queue: queue.Queue = queue.Queue(maxsize=-1)
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
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.debug(f"{src.value}: {str(message).strip()}", exc_info=exc_info)

    def info(
        self, message: str, source: LogSource | None = None, exc_info: bool = False
    ) -> None:
        if self.logger.level <= logging.INFO:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.info(f"{src.value}: {str(message).strip()}", exc_info=exc_info)

    def warning(
        self, message: str, source: LogSource | None = None, exc_info: bool = False
    ) -> None:
        if self.logger.level <= logging.WARNING:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.warning(
                f"{src.value}: {str(message).strip()}", exc_info=exc_info
            )

    def error(
        self, message: str, source: LogSource | None = None, exc_info: bool = False
    ) -> None:
        if self.logger.level <= logging.ERROR:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.error(f"{src.value}: {str(message).strip()}", exc_info=exc_info)

    def critical(
        self, message: str, source: LogSource | None = None, exc_info: bool = False
    ) -> None:
        if self.logger.level <= logging.CRITICAL:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.critical(
                f"{src.value}: {str(message).strip()}", exc_info=exc_info
            )

    def exception(self, message: str, source: LogSource | None = None) -> None:
        """Log exception with traceback (use within except block)"""
        if self.logger.level <= logging.ERROR:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.exception(f"{src.value}: {str(message).strip()}")

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
LOG = Logger(_log_path, to_console=True)
