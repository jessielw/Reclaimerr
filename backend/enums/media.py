from enum import StrEnum, auto


class Service(StrEnum):
    SONARR = auto()
    RADARR = auto()
    JELLYFIN = auto()
    EMBY = auto()
    PLEX = auto()
    SEERR = auto()
    TAUTULLI = auto()
    TRACEARR = auto()
    MDBLIST = auto()
    OMDB = auto()


class MediaType(StrEnum):
    MOVIE = auto()
    SERIES = auto()


class ProtectionRequestStatus(StrEnum):
    PENDING = auto()
    APPROVED = auto()
    DENIED = auto()


class LeavingSoonCollectionSort(StrEnum):
    """How items are ordered inside a managed Leaving Soon collection.

    Only Plex can honor this. Emby and Jellyfin expose no custom BoxSet
    ordering, so their clients accept the value and ignore it.
    """

    DEFAULT = auto()
    ALPHA = auto()
    LEAVING_SOONEST = auto()
