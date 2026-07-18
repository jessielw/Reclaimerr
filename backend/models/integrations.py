from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from backend.enums import MediaType

ApiTokenScope = Literal[
    "candidates:read",
    "candidates:manage",
    "events:read",
    "media:read",
    "protections:read",
    "protections:manage",
    "system:read",
    "tasks:read",
    "tasks:run",
]
LifecycleEventType = Literal[
    "candidate.scheduled",
    "candidate.canceled",
    "candidate.postponed",
    "candidate.timer_reset",
    "candidate.protected",
    "candidate.deleted",
    "candidate.moved",
    "protection.created",
    "protection.removed",
]


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[ApiTokenScope] = Field(min_length=1)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def normalize(self) -> ApiTokenCreate:
        self.name = self.name.strip()
        self.scopes = list(dict.fromkeys(self.scopes))
        return self


class ApiTokenResponse(BaseModel):
    id: int
    name: str
    token_prefix: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiTokenCreatedResponse(ApiTokenResponse):
    token: str


class WebhookHeaderInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=4096)


class WebhookEndpointInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    method: Literal["GET", "POST"] = "POST"
    url_template: str = Field(min_length=1, max_length=2048)
    event_types: list[LifecycleEventType] = Field(min_length=1)
    media_types: list[MediaType] = Field(
        default_factory=lambda: [MediaType.MOVIE, MediaType.SERIES]
    )
    path_mode: Literal["original", "local", "destination"] = "original"
    body_template: str | None = None
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    auth_username: str | None = None
    auth_password: str | None = None
    headers: list[WebhookHeaderInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize(self) -> WebhookEndpointInput:
        self.name = self.name.strip()
        self.url_template = self.url_template.strip()
        if not self.url_template.startswith(("http://", "https://")):
            raise ValueError("Webhook URL must start with http:// or https://")
        self.event_types = list(dict.fromkeys(self.event_types))
        self.media_types = list(dict.fromkeys(self.media_types))
        return self


class WebhookEndpointResponse(BaseModel):
    id: int
    name: str
    enabled: bool
    method: str
    url_template: str
    event_types: list[str]
    media_types: list[str]
    path_mode: str
    body_template: str | None
    timeout_seconds: int
    auth_username: str | None
    auth_password_is_set: bool
    headers: list[WebhookHeaderInput]
    created_at: datetime
    updated_at: datetime


class WebhookDeliveryResponse(BaseModel):
    id: int
    event_id: str
    event_type: str
    endpoint_id: int
    endpoint_name: str
    status: str
    attempts: int
    next_attempt_at: datetime
    last_status_code: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime


class WebhookTestResponse(BaseModel):
    success: bool
    status_code: int | None = None
    error: str | None = None
