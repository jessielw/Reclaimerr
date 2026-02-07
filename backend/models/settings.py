from typing import Literal

from pydantic import BaseModel, model_validator

from backend.enums import Service


class ServiceConfigUpdate(BaseModel):
    service_type: Service
    base_url: str
    api_key: str
    enabled: bool
    libraries: list[dict] | None = None

    @model_validator(mode="after")
    def sanitize_fields(self) -> "ServiceConfigUpdate":
        """Sanitize fields after model initialization."""
        self.base_url = self.base_url.strip()
        self.api_key = self.api_key.strip()
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
    def sanitize_fields(self) -> "NotificationSettingItem":
        """Sanitize fields after model initialization."""
        self.url = self.url.strip()
        return self


class NotificationTestRequest(BaseModel):
    url: str

    @model_validator(mode="after")
    def sanitize_fields(self) -> "NotificationTestRequest":
        """Sanitize fields after model initialization."""
        self.url = self.url.strip()
        return self
