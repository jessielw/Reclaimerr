from enum import Enum


class SeerrRequestStatus(Enum):
    "https://github.com/seerr-team/seerr/blob/develop/seerr-api.yml"

    PENDING = 1
    APPROVED = 2
    DECLINED = 3
    FAILED = 4
    COMPLETED = 5
