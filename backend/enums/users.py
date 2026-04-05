from enum import StrEnum, auto


class UserRole(StrEnum):
    USER = auto()
    ADMIN = auto()


class Permission(StrEnum):
    MANAGE_USERS = auto()
    MANAGE_REQUESTS = auto()
    REQUEST = auto()
    AUTO_APPROVE = auto()
    MANAGE_PROTECTION = auto()
    MANAGE_RECLAIM = auto()
