from enum import Enum, StrEnum, auto


class Service(StrEnum):
    SONARR = auto()
    RADARR = auto()
    JELLYFIN = auto()
    PLEX = auto()
    SEERR = auto()


class MediaType(StrEnum):
    MOVIE = auto()
    SERIES = auto()


class SeriesStatus(StrEnum):
    CONTINUING = auto()
    ENDED = auto()


class TaskStatus(StrEnum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()


class SeerrRequestStatus(Enum):
    "https://github.com/seerr-team/seerr/blob/develop/seerr-api.yml"

    PENDING = 1
    APPROVED = 2
    DECLINED = 3
    FAILED = 4
    COMPLETED = 5
