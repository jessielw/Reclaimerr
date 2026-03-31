from enum import StrEnum, auto


class Service(StrEnum):
    SONARR = auto()
    RADARR = auto()
    JELLYFIN = auto()
    PLEX = auto()
    SEERR = auto()


class MediaType(StrEnum):
    MOVIE = auto()
    SERIES = auto()


class ProtectionRequestStatus(StrEnum):
    PENDING = auto()
    APPROVED = auto()
    DENIED = auto()
