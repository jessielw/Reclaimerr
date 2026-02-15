from enum import Enum, StrEnum, auto


class UserRole(StrEnum):
    USER = auto()
    ADMIN = auto()


class Service(StrEnum):
    SONARR = auto()
    RADARR = auto()
    JELLYFIN = auto()
    PLEX = auto()
    SEERR = auto()


class MediaType(StrEnum):
    MOVIE = auto()
    SERIES = auto()


class TaskStatus(StrEnum):
    SCHEDULED = auto()
    COMPLETED = auto()
    ERROR = auto()
    RUNNING = auto()
    DISABLED = auto()


class SeerrRequestStatus(Enum):
    "https://github.com/seerr-team/seerr/blob/develop/seerr-api.yml"

    PENDING = 1
    APPROVED = 2
    DECLINED = 3
    FAILED = 4
    COMPLETED = 5


class NotificationType(StrEnum):
    NEW_CLEANUP_CANDIDATES = auto()
    REQUEST_APPROVED = auto()
    REQUEST_DECLINED = auto()
    ADMIN_MESSAGE = auto()

    # admin exclusive notifications
    TASK_FAILURE = auto()

    def is_admin_only(self) -> bool:
        """Check if this notification type is restricted to admins."""
        return self in (NotificationType.TASK_FAILURE,)


class ScheduleType(StrEnum):
    INTERVAL = auto()
    CRON = auto()


class Task(StrEnum):
    # sync
    SYNC_ALL_MEDIA = auto()
    SYNC_SERVICE_LIBRARIES = auto()

    # cleanup
    SCAN_CLEANUP_CANDIDATES = auto()
    TAG_CLEANUP_CANDIDATES = auto()
    DELETE_CLEANUP_CANDIDATES = auto()

    # housekeeping
    WEEKLY_HOUSE_KEEPING = auto()

    def friendly_name(self) -> str:
        return self.name.replace("_", " ").title()
