import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend.core.__version__ import __version__, program_name
from backend.core.logger.enums import LogSource
from backend.core.logger.levels import LogLevel
from backend.core.settings import settings


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
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(log_level.value)
        self.log_file = log_file
        self.log_level = log_level
        self.file_handler = None
        self.console_handler = None
        self.to_console = to_console
        self.default_source = default_source

        log_file.parent.mkdir(parents=True, exist_ok=True)

    def _initialize_file_handler(self) -> None:
        # already initialized, nothing to do
        if self.file_handler is not None:
            return

        # initialize file handler
        self.file_handler = RotatingFileHandler(
            self.log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        self.file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        self.logger.addHandler(self.file_handler)

        # initialize console handler if needed
        if self.to_console:
            self.console_handler = logging.StreamHandler()
            self.console_handler.setFormatter(
                logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
            )
            self.logger.addHandler(self.console_handler)

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


# initialize global logger instance with a static filename
_log_filename = f"{program_name.lower().replace(' ', '_')}.log"
_log_path = settings.log_dir / _log_filename
LOG = Logger(_log_path, to_console=True)
