from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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
    id: int | None = None
    name: str | None = None
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
        if self.name is not None:
            self.name = self.name.strip()
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


class PathMappingItem(BaseModel):
    source_prefix: str
    local_prefix: str


class GeneralSettingsResponse(BaseModel):
    worker_poll_min_seconds: float | None = None
    worker_poll_max_seconds: float | None = None

    # path mappings for resolving media-server paths to local paths
    path_mappings: list[PathMappingItem] = Field(default_factory=list)

    # move settings
    move_enabled: bool = False
    move_destination_movies: str | None = None
    move_destination_series: str | None = None

    # metadata (only updated on PUT, not required on GET)
    updated_at: datetime | None = None
    updated_by: User | None = None

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
