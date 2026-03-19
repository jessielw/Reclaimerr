from enum import Enum, StrEnum

from typing_extensions import override


class LogSource(StrEnum):
    """
    Enum to control tag for frontend vs backend
    FE: Frontend
    BE: Backend
    """

    FE = "[FE]"
    BE = "[BE]"


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
