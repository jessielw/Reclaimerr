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

    def friendly_name(self) -> str:
        return self.name.replace("_", " ").title()
