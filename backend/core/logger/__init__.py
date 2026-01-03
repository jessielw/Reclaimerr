import logging
import sys
from datetime import datetime
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path

import shortuuid
from typing_extensions import override

from backend.core.__version__ import __version__, program_name
from backend.core.utils.working_dir import RUNTIME_DIR


class LogLevel(Enum):
    """Enum class for pythons built in logging class debug types"""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50

    @override
    def __str__(self) -> str:
        level_map = {
            LogLevel.DEBUG: "Debug",
            LogLevel.INFO: "Info",
            LogLevel.WARNING: "Warning",
            LogLevel.ERROR: "Error",
            LogLevel.CRITICAL: "Critical",
        }
        return level_map[self]


class LogSource(Enum):
    """
    Enum to control tag for frontend vs backend
    FE: Frontend
    BE: Backend
    """

    FE = "[FE]"
    BE = "[BE]"


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

    def debug(self, message: str, source: LogSource | None = None) -> None:
        if self.logger.level <= logging.DEBUG:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.debug(f"{src.value}: {str(message).strip()}")

    def info(self, message: str, source: LogSource | None = None) -> None:
        if self.logger.level <= logging.INFO:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.info(f"{src.value}: {str(message).strip()}")

    def warning(self, message: str, source: LogSource | None = None) -> None:
        if self.logger.level <= logging.WARNING:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.warning(f"{src.value}: {str(message).strip()}")

    def error(self, message: str, source: LogSource | None = None) -> None:
        if self.logger.level <= logging.ERROR:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.error(f"{src.value}: {str(message).strip()}")

    def critical(self, message: str, source: LogSource | None = None) -> None:
        if self.logger.level <= logging.CRITICAL:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.critical(f"{src.value}: {str(message).strip()}")

    def exception(self, message: str, source: LogSource | None = None) -> None:
        """Log exception with traceback (use within except block)"""
        if self.logger.level <= logging.ERROR:
            self._initialize_file_handler()
            src = source or self.default_source
            self.logger.exception(f"{src.value}: {str(message).strip()}")

    def set_log_level(self, log_level: LogLevel) -> None:
        self.logger.setLevel(log_level.value)

    def clean_up_logs(self, max_logs: int) -> None:
        log_files = list(self.log_file.parent.glob("*.log"))
        total_files = len(log_files)

        # sort log files by extracting the timestamp part of the filename
        log_files.sort(
            key=lambda f: datetime.strptime(
                f.name.split("_")[1] + "_" + f.name.split("_")[2], "%Y-%m-%d_%H-%M-%S"
            )
        )

        if total_files > max_logs:
            files_to_delete = log_files[: total_files - max_logs]

            for del_file in files_to_delete:
                del_file.unlink()


_date_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
_short_uuid = shortuuid.uuid()[:7]
_log_filename = (
    f"{program_name.lower().replace(' ', '_')}_{_date_time_str}_{_short_uuid}.log"
)
_log_path = RUNTIME_DIR / "logs" / _log_filename
_to_console = "debug" in sys.executable.lower()
LOG = Logger(_log_path, to_console=_to_console)
