from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from backend.database.models import User
from backend.enums import MediaType, NotificationType, Service
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


def default_notification_preferences() -> dict[str, dict[str, Any]]:
    """Return default notification formatting preferences."""
    return {
        NotificationType.NEW_CLEANUP_CANDIDATES.value: {
            "detail": "top_n_summary",
            "max_items": 5,
        },
        NotificationType.REQUEST_APPROVED.value: {"detail": "standard"},
        NotificationType.REQUEST_DECLINED.value: {"detail": "standard"},
        NotificationType.ADMIN_MESSAGE.value: {"detail": "standard"},
        NotificationType.TASK_FAILURE.value: {"detail": "standard"},
    }


def normalize_notification_preferences(
    preferences: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Normalize and validate notification formatting preferences."""
    normalized = default_notification_preferences()
    if not isinstance(preferences, dict):
        return normalized

    cleanup_key = NotificationType.NEW_CLEANUP_CANDIDATES.value
    cleanup_raw = preferences.get(cleanup_key)
    if isinstance(cleanup_raw, dict):
        detail = str(cleanup_raw.get("detail") or "").strip().lower()
        if detail in {"count_only", "top_n_summary", "top_n_with_reasons"}:
            normalized[cleanup_key]["detail"] = detail
        max_items_raw = cleanup_raw.get("max_items")
        try:
            max_items = (
                int(max_items_raw)
                if max_items_raw is not None
                else normalized[cleanup_key]["max_items"]
            )
            normalized[cleanup_key]["max_items"] = min(max(max_items, 1), 20)
        except (TypeError, ValueError):
            pass

    for notif_type in (
        NotificationType.REQUEST_APPROVED,
        NotificationType.REQUEST_DECLINED,
        NotificationType.ADMIN_MESSAGE,
        NotificationType.TASK_FAILURE,
    ):
        key = notif_type.value
        raw = preferences.get(key)
        if not isinstance(raw, dict):
            continue
        detail = str(raw.get("detail") or "").strip().lower()
        if detail in {"compact", "standard"}:
            normalized[key]["detail"] = detail

    return normalized


class NotificationSettingItem(BaseModel):
    """Model for creating or updating notification settings."""

    id: int | None = None  # None for new, populated for updates
    enabled: bool = True
    name: str | None = None
    url: str
    new_cleanup_candidates: bool = False
    request_approved: bool = False
    request_declined: bool = False
    admin_message: bool = False
    task_failure: bool = False
    preferences: dict[str, dict[str, Any]] = Field(
        default_factory=default_notification_preferences
    )

    @model_validator(mode="after")
    def sanitize_fields(self) -> NotificationSettingItem:
        """Sanitize and validate notification URL."""
        self.url = self.url.strip()
        _validate_notification_url(self.url)
        self.preferences = normalize_notification_preferences(self.preferences)
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
    service_type: Service | None = None
    service_config_id: int | None = None

    @model_validator(mode="after")
    def sanitize_fields(self) -> PathMappingItem:
        self.source_prefix = self.source_prefix.strip()
        self.local_prefix = self.local_prefix.strip()
        return self


class PostActionWebhookHeader(BaseModel):
    name: str
    value: str

    @model_validator(mode="after")
    def sanitize_fields(self) -> PostActionWebhookHeader:
        self.name = self.name.strip()
        self.value = self.value.strip()
        if not self.name:
            raise PydanticCustomError("webhook_header", "Header name is required")
        return self


class PostActionWebhookConfig(BaseModel):
    enabled: bool = True
    name: str
    method: Literal["GET", "POST"] = "GET"
    url_template: str
    headers: list[PostActionWebhookHeader] = Field(default_factory=list)
    auth_username: str | None = None
    auth_password: str | None = None
    actions: list[Literal["deleted", "moved"]] = Field(
        default_factory=lambda: ["deleted", "moved"]
    )
    media_types: list[MediaType] = Field(
        default_factory=lambda: [MediaType.MOVIE, MediaType.SERIES]
    )
    path_mode: Literal["original", "local", "destination"] = "original"
    body_template: str | None = None
    timeout_seconds: int = Field(default=15, ge=1, le=120)

    @model_validator(mode="after")
    def sanitize_fields(self) -> PostActionWebhookConfig:
        self.name = self.name.strip() or "Post-action webhook"
        self.method = self.method.upper()  # type: ignore[assignment]
        self.url_template = self.url_template.strip()
        if not self.url_template:
            raise PydanticCustomError("webhook_url", "Webhook URL is required")
        if not (
            self.url_template.startswith("http://")
            or self.url_template.startswith("https://")
        ):
            raise PydanticCustomError(
                "webhook_url", "Webhook URL must start with http:// or https://"
            )
        if self.auth_username is not None:
            self.auth_username = self.auth_username.strip() or None
        if self.auth_password is not None:
            self.auth_password = self.auth_password.strip() or None
        if self.body_template is not None:
            self.body_template = self.body_template.strip() or None
        return self


class PostActionWebhookTestRequest(BaseModel):
    webhook: PostActionWebhookConfig


class PostActionWebhookTestResponse(BaseModel):
    success: bool
    status_code: int | None = None
    error: str | None = None


class FavoritesUserLookupResponse(BaseModel):
    username: str
    has_favorites: bool
    favorites_count: int
    source_count: int
    sources: list[str] = Field(default_factory=list)


class GeneralSettingsResponse(BaseModel):
    worker_poll_min_seconds: float | None = None
    worker_poll_max_seconds: float | None = None

    # path mappings for resolving media-server paths to local paths
    path_mappings: list[PathMappingItem] = Field(default_factory=list)

    # post action webhooks for delete/move integrations (Autopulse, scripts, etc.)
    post_action_webhooks: list[PostActionWebhookConfig] = Field(default_factory=list)

    # move settings
    move_enabled: bool = False
    move_destination_movies: str | None = None
    move_destination_series: str | None = None

    # deletion routing
    media_server_fallback_enabled: bool = True
    default_arr_delete_behavior: Literal["unmonitor", "remove_if_empty"] = "unmonitor"
    add_arr_import_exclusions_on_delete: bool = True

    # favorites
    favorites_ignore_enabled: bool = False
    favorites_protect_all_users: bool = False
    favorites_usernames: list[str] = Field(default_factory=list)

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

    @model_validator(mode="after")
    def normalize_usernames(self) -> GeneralSettingsResponse:
        """Normalize usernames in favorites_usernames."""
        normalized_usernames: list[str] = []
        seen: set[str] = set()
        for raw in self.favorites_usernames:
            value = str(raw).strip().lower()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized_usernames.append(value)
        self.favorites_usernames = normalized_usernames
        return self

    model_config = ConfigDict(from_attributes=True)
