from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from backend.database.models import User
from backend.enums import MediaType, NotificationType, PageAccess, Service
from backend.user_types import (
    DEFAULT_NEW_USER_ALLOWED_PAGES,
    MEDIA_SERVERS,
    MediaServerType,
)
from backend.utils.helpers import normalize_leaving_soon_collection_title


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
    libraries: list[dict[str, Any]] | None = None

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
        NotificationType.ADMIN_NEW_DELETE_REQUEST.value: {"detail": "standard"},
        NotificationType.ADMIN_NEW_PROTECTION_REQUEST.value: {"detail": "standard"},
        NotificationType.ADMIN_REQUEST_CANCELLED.value: {"detail": "standard"},
        NotificationType.ADMIN_DELETE_EXECUTION_FAILED.value: {"detail": "standard"},
        NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED.value: {
            "detail": "standard"
        },
        NotificationType.DELETE_REQUEST_EXECUTION_FAILED.value: {"detail": "standard"},
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
        NotificationType.ADMIN_NEW_DELETE_REQUEST,
        NotificationType.ADMIN_NEW_PROTECTION_REQUEST,
        NotificationType.ADMIN_REQUEST_CANCELLED,
        NotificationType.ADMIN_DELETE_EXECUTION_FAILED,
        NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED,
        NotificationType.DELETE_REQUEST_EXECUTION_FAILED,
    ):
        key = notif_type.value
        raw = preferences.get(key)
        if not isinstance(raw, dict):
            continue
        detail = str(raw.get("detail") or "").strip().lower()
        if detail in {"compact", "standard"}:
            normalized[key]["detail"] = detail

    return normalized


def default_post_action_webhook_actions() -> list[Literal["deleted", "moved"]]:
    return ["deleted", "moved"]


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
    admin_new_delete_request: bool = False
    admin_new_protection_request: bool = False
    admin_request_cancelled: bool = False
    admin_delete_execution_failed: bool = False
    delete_request_execution_succeeded: bool = False
    delete_request_execution_failed: bool = False
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


class RequesterWatchUserMapping(BaseModel):
    """Map Seerr requester identities to media server watch user keys."""

    seerr_user_id: int | None = None
    seerr_username: str | None = None
    media_user_key: str
    service_type: Service | None = None

    @model_validator(mode="after")
    def normalize_fields(self) -> RequesterWatchUserMapping:
        if self.seerr_username is not None:
            self.seerr_username = self.seerr_username.strip().lower() or None
        self.media_user_key = self.media_user_key.strip()
        if not self.media_user_key:
            raise PydanticCustomError(
                "requester_watch_user_mapping",
                "media_user_key is required",
            )
        if self.seerr_user_id is None and self.seerr_username is None:
            raise PydanticCustomError(
                "requester_watch_user_mapping",
                "Either seerr_user_id or seerr_username is required",
            )
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
        default_factory=default_post_action_webhook_actions
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


class WatchUserLookupResponse(BaseModel):
    user_key: str
    user_key_normalized: str
    source_services: list[Service] = Field(default_factory=list)


class FavoritesMediaEntryResponse(BaseModel):
    media_type: MediaType
    tmdb_id: int
    title: str
    year: int | None = None
    poster_url: str | None = None
    favorite_user_count: int
    favorite_users: list[str] = Field(default_factory=list)
    is_missing_metadata: bool = False


class PaginatedFavoritesMediaResponse(BaseModel):
    items: list[FavoritesMediaEntryResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class GeneralSettingsResponse(BaseModel):
    worker_poll_min_seconds: float | None = None
    worker_poll_max_seconds: float | None = None

    # path mappings for resolving media-server paths to local paths
    path_mappings: list[PathMappingItem] = Field(default_factory=list)

    # post action webhooks for delete/move integrations (Autopulse, scripts, etc.)
    post_action_webhooks: list[PostActionWebhookConfig] = Field(default_factory=list)

    # move destinations
    move_destination_movies: str | None = None
    move_destination_series: str | None = None

    # deletion routing
    media_server_fallback_enabled: bool = True
    default_arr_delete_behavior: Literal["unmonitor", "remove_if_empty"] = "unmonitor"
    add_arr_import_exclusions_on_delete: bool = True
    auto_delete_movie_delay_days: int = Field(default=14, ge=0, le=3650)
    auto_delete_series_delay_days: int = Field(default=7, ge=0, le=3650)
    application_url: str | None = None

    # favorites
    favorites_ignore_enabled: bool = False
    favorites_protect_all_users: bool = False
    favorites_usernames: list[str] = Field(default_factory=list)
    requester_watch_user_mappings: list[RequesterWatchUserMapping] = Field(
        default_factory=list
    )
    default_allowed_pages: list[PageAccess] = Field(
        default_factory=lambda: [
            PageAccess(page) for page in DEFAULT_NEW_USER_ALLOWED_PAGES
        ]
    )
    leaving_soon_enabled: bool = False
    leaving_soon_collection_title: str = "Leaving Soon"

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

    @model_validator(mode="after")
    def normalize_leaving_soon_title(self) -> GeneralSettingsResponse:
        title = normalize_leaving_soon_collection_title(
            self.leaving_soon_collection_title
        )
        self.leaving_soon_collection_title = title
        return self

    @model_validator(mode="after")
    def normalize_requester_watch_user_mappings(self) -> GeneralSettingsResponse:
        """Normalize and dedupe requester watch user mappings."""
        deduped: list[RequesterWatchUserMapping] = []
        seen: set[tuple[int | None, str | None, str, Service | None]] = set()
        for mapping in self.requester_watch_user_mappings:
            key = (
                mapping.seerr_user_id,
                mapping.seerr_username,
                mapping.media_user_key.strip().lower(),
                mapping.service_type,
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(mapping)
        self.requester_watch_user_mappings = deduped
        return self

    @model_validator(mode="after")
    def normalize_default_allowed_pages(self) -> GeneralSettingsResponse:
        self.default_allowed_pages = list(dict.fromkeys(self.default_allowed_pages))
        if not self.default_allowed_pages:
            raise PydanticCustomError(
                "default_allowed_pages",
                "At least one default page must be selected",
            )
        return self

    @model_validator(mode="after")
    def normalize_application_url(self) -> GeneralSettingsResponse:
        value = self.application_url
        if value is None:
            return self

        normalized = value.strip().rstrip("/")
        if not normalized:
            self.application_url = None
            return self

        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PydanticCustomError(
                "application_url",
                "Application URL must be an absolute http:// or https:// URL",
            )

        self.application_url = normalized
        return self

    model_config = ConfigDict(from_attributes=True)


class OIDCSettingsResponse(BaseModel):
    enabled: bool = False
    issuer_url: str = ""
    client_id: str = ""
    scopes: str = "openid profile email"
    email_claim: str = "email"
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post"] = (
        "client_secret_basic"
    )
    redirect_uri_override: str | None = None
    client_secret_configured: bool = False
    updated_at: datetime | None = None
    updated_by: User | None = None

    @model_validator(mode="after")
    def sanitize_fields(self) -> OIDCSettingsResponse:
        self.issuer_url = self.issuer_url.strip().rstrip("/")
        self.client_id = self.client_id.strip()
        self.scopes = " ".join(self.scopes.split()) or "openid profile email"
        self.email_claim = self.email_claim.strip() or "email"
        self.redirect_uri_override = (
            self.redirect_uri_override.strip()
            if self.redirect_uri_override is not None
            else None
        ) or None
        return self


class OIDCSettingsUpdate(BaseModel):
    enabled: bool = False
    issuer_url: str = ""
    client_id: str = ""
    client_secret: str | None = None  # None = keep existing secret
    scopes: str = "openid profile email"
    email_claim: str = "email"
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post"] = (
        "client_secret_basic"
    )
    redirect_uri_override: str | None = None

    @model_validator(mode="after")
    def sanitize_fields(self) -> OIDCSettingsUpdate:
        self.issuer_url = self.issuer_url.strip().rstrip("/")
        self.client_id = self.client_id.strip()
        self.client_secret = (
            self.client_secret.strip() if self.client_secret is not None else None
        ) or None
        self.scopes = " ".join(self.scopes.split()) or "openid profile email"
        self.email_claim = self.email_claim.strip() or "email"
        self.redirect_uri_override = (
            self.redirect_uri_override.strip()
            if self.redirect_uri_override is not None
            else None
        ) or None
        return self


class OIDCTestResponse(BaseModel):
    success: bool
    issuer: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    jwks_uri: str | None = None
    userinfo_endpoint: str | None = None
