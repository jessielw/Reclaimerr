from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_core import PydanticCustomError

from backend.database.models import User
from backend.enums import Service
from backend.types import MEDIA_SERVERS, MediaServerType


def _validate_notification_url(url: str) -> None:
    """Basic sanity check that a notification URL has a recognizable scheme."""
    if url and "://" not in url:
        raise PydanticCustomError(
            "notification_url",
            "Notification URL must include a scheme (e.g. discord://, https://, ntfy://)",
        )


class ServiceConfigUpdate(BaseModel):
    service_type: Service
    base_url: str
    api_key: str | None = None  # None = keep existing key (used if frontend changes it)
    enabled: bool
    extra_settings: dict[str, Any] | None = None
    # only plex/jellyfin/emby are eligible; at most one server may have this True
    is_main: bool = False
    libraries: list[dict] | None = None

    @model_validator(mode="after")
    def sanitize_fields(self) -> ServiceConfigUpdate:
        """Sanitize fields after model initialization."""
        self.base_url = self.base_url.strip()
        if self.api_key is not None:
            self.api_key = self.api_key.strip() or None  # treat empty string as None
        if self.is_main and self.service_type not in MEDIA_SERVERS:
            raise PydanticCustomError(
                "is_main_invalid",
                "Only Plex, Jellyfin, and Emby can be designated as the main media server",
            )
        return self


class UpdateMediaLibrariesRequest(BaseModel):
    service_type: MediaServerType | None = None


class LibrarySelectionUpdate(BaseModel):
    id: int
    selected: bool


class NotificationSettingItem(BaseModel):
    """Model for creating or updating notification settings."""

    id: int | None = None  # None for new, populated for updates
    enabled: bool
    name: str | None = None
    url: str
    new_cleanup_candidates: bool = False
    request_approved: bool = False
    request_declined: bool = False
    admin_message: bool = False
    task_failure: bool = False

    @model_validator(mode="after")
    def sanitize_fields(self) -> NotificationSettingItem:
        """Sanitize and validate notification URL."""
        self.url = self.url.strip()
        _validate_notification_url(self.url)
        return self


class NotificationTestRequest(BaseModel):
    url: str

    @model_validator(mode="after")
    def sanitize_fields(self) -> NotificationTestRequest:
        """Sanitize and validate notification URL."""
        self.url = self.url.strip()
        _validate_notification_url(self.url)
        return self


class GeneralSettingsResponse(BaseModel):
    auto_tag_enabled: bool
    cleanup_tag_suffix: str
    worker_poll_min_seconds: float | None = None
    worker_poll_max_seconds: float | None = None

    # metadata (only updated on PUT, not required on GET)
    updated_at: datetime | None = None
    updated_by: User | None = None

    @field_validator("cleanup_tag_suffix", mode="before")
    @classmethod
    def validate_cleanup_tag_suffix(cls, v: str) -> str:
        """Ensure cleanup tag suffix is formatted correctly."""
        if not v:
            return ""

        # remove leading/trailing whitespace + convert to lowercase
        v = v.strip().lower()

        # enforce max length of 15 characters
        if len(v) > 15:
            raise PydanticCustomError(
                "cleanup_tag_suffix", "Cleanup tag suffix cannot exceed 15 characters"
            )

        # enforce starting with hyphen or underscore
        if not v.startswith("-") and not v.startswith("_"):
            raise PydanticCustomError(
                "cleanup_tag_suffix",
                "Cleanup tag suffix must start with a hyphen or an underscore (e.g. '-candidate')",
            )

        # if any whitespace or characters other than hyphen, underscore, and lowercase letters are found
        # raise an error
        if re.search(r"[^a-z-_]", v):
            raise PydanticCustomError(
                "cleanup_tag_suffix",
                "Cleanup tag suffix can only contain lowercase letters, hyphens, and underscores",
            )

        return v

    @field_validator("worker_poll_min_seconds", "worker_poll_max_seconds")
    @classmethod
    def validate_worker_poll_seconds(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if v <= 0:
            raise PydanticCustomError(
                "worker_poll_seconds",
                "Worker polling values must be greater than 0",
            )
        if v > 60:
            raise PydanticCustomError(
                "worker_poll_seconds",
                "Worker polling values cannot exceed 60 seconds",
            )
        return float(v)

    @model_validator(mode="after")
    def validate_worker_poll_range(self) -> GeneralSettingsResponse:
        if (
            self.worker_poll_min_seconds is not None
            and self.worker_poll_max_seconds is not None
            and self.worker_poll_min_seconds > self.worker_poll_max_seconds
        ):
            raise PydanticCustomError(
                "worker_poll_range",
                "Worker poll min seconds cannot be greater than worker poll max seconds",
            )
        return self

    model_config = ConfigDict(from_attributes=True)
