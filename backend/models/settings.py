from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_core import PydanticCustomError

from backend.database.models import User
from backend.enums import Service


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
    libraries: list[dict] | None = None

    @model_validator(mode="after")
    def sanitize_fields(self) -> ServiceConfigUpdate:
        """Sanitize fields after model initialization."""
        self.base_url = self.base_url.strip()
        if self.api_key is not None:
            self.api_key = self.api_key.strip() or None  # treat empty string as None
        return self


class UpdateMediaLibrariesRequest(BaseModel):
    service_type: Literal[Service.PLEX, Service.JELLYFIN] | None = None


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

    model_config = ConfigDict(from_attributes=True)
