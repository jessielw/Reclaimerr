from enum import StrEnum, auto


class TaskStatus(StrEnum):
    SCHEDULED = auto()
    QUEUED = auto()
    COMPLETED = auto()
    ERROR = auto()
    RUNNING = auto()
    DISABLED = auto()


class BackgroundJobStatus(StrEnum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELED = auto()


class BackgroundJobType(StrEnum):
    SERVICE_TOGGLE = auto()
    TASK_RUN = auto()
    CANDIDATE_FILE_OP = auto()
    WEBHOOK_DELIVERY = auto()


class CandidateFileOpOperation(StrEnum):
    DELETE = auto()
    MOVE = auto()


class NotificationType(StrEnum):
    NEW_CLEANUP_CANDIDATES = auto()
    REQUEST_APPROVED = auto()
    REQUEST_DECLINED = auto()
    ADMIN_MESSAGE = auto()
    DELETE_REQUEST_EXECUTION_SUCCEEDED = auto()
    DELETE_REQUEST_EXECUTION_FAILED = auto()

    # admin exclusive notifications
    TASK_FAILURE = auto()
    ADMIN_NEW_DELETE_REQUEST = auto()
    ADMIN_NEW_PROTECTION_REQUEST = auto()
    ADMIN_REQUEST_CANCELLED = auto()
    ADMIN_DELETE_EXECUTION_FAILED = auto()

    def is_admin_only(self) -> bool:
        """Check if this notification type is restricted to admins."""
        return self in {
            NotificationType.ADMIN_MESSAGE,
            NotificationType.TASK_FAILURE,
            NotificationType.ADMIN_NEW_DELETE_REQUEST,
            NotificationType.ADMIN_NEW_PROTECTION_REQUEST,
            NotificationType.ADMIN_REQUEST_CANCELLED,
            NotificationType.ADMIN_DELETE_EXECUTION_FAILED,
        }


class ScheduleType(StrEnum):
    INTERVAL = auto()
    CRON = auto()
    MANUAL = auto()


class Task(StrEnum):
    # automatic sync tasks
    # sync media as well as run any other sync tasks needed
    SYNC_MEDIA = auto()
    RESYNC_MEDIA = auto()

    # manual only sync tasks (run only when manually triggered)
    SYNC_MEDIA_LIBRARIES = auto()
    SYNC_LINKED_DATA = auto()

    # cleanup
    SCAN_CLEANUP_CANDIDATES = auto()
    TAG_CLEANUP_CANDIDATES = auto()
    DELETE_CLEANUP_CANDIDATES = auto()

    # housekeeping
    WEEKLY_HOUSE_KEEPING = auto()

    # misc
    CHECK_APP_UPDATES = auto()
    IMDB_RATINGS_REFRESH = auto()
    ANILIST_RATINGS_REFRESH = auto()
    MDBLIST_RATINGS_REFRESH = auto()
    OMDB_RATINGS_REFRESH = auto()
    REFRESH_PLAYBACK_HISTORY = auto()

    def friendly_name(self) -> str:
        branded_names = {
            Task.IMDB_RATINGS_REFRESH: "Refresh IMDb Ratings",
            Task.ANILIST_RATINGS_REFRESH: "Refresh AniList Ratings",
            Task.MDBLIST_RATINGS_REFRESH: "Refresh MDBList Ratings",
            Task.OMDB_RATINGS_REFRESH: "Refresh OMDb Ratings",
        }
        if self in branded_names:
            return branded_names[self]
        return self.name.replace("_", " ").title()
