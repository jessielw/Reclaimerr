"""Credential-free contracts shared by the UI and versioned user import API."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from backend.enums import PageAccess, Permission, Service, UserRole


class MediaImportProvider(BaseModel):
    service_config_id: int
    service_type: Service
    name: str


class MediaImportLink(BaseModel):
    source_user_id: str = Field(min_length=1, max_length=255)
    target_user_id: int = Field(gt=0)


class MediaIdentityDiscoveryRequest(BaseModel):
    service_config_id: int = Field(gt=0)
    source_user_id: str = Field(min_length=1, max_length=255)


class MediaUserImportRequest(BaseModel):
    service_config_id: int = Field(gt=0)
    source_user_ids: list[str] | None = Field(default=None, max_length=1000)
    links: list[MediaImportLink] = Field(default_factory=list, max_length=1000)
    role: UserRole = UserRole.USER
    permissions: list[Permission] = Field(default_factory=lambda: [Permission.REQUEST])
    use_default_page_access: bool = True
    allowed_pages: list[PageAccess] | None = None
    expected_revisions: dict[str, str] = Field(default_factory=dict, max_length=1000)

    @model_validator(mode="after")
    def validate_selection(self) -> "MediaUserImportRequest":
        if self.source_user_ids is not None:
            if any(
                not item.strip() or len(item) > 255 for item in self.source_user_ids
            ):
                raise ValueError("Source user IDs must contain 1–255 characters")
            self.source_user_ids = list(dict.fromkeys(self.source_user_ids))
        ids = [link.source_user_id for link in self.links]
        if len(ids) != len(set(ids)):
            raise ValueError("Choose only one local target for each media user")
        if self.source_user_ids is not None and not set(ids) <= set(
            self.source_user_ids
        ):
            raise ValueError("Link targets must belong to the selected source users")
        self.permissions = list(dict.fromkeys(self.permissions))
        if self.allowed_pages is not None:
            self.allowed_pages = list(dict.fromkeys(self.allowed_pages))
        return self


class MediaImportAccount(BaseModel):
    id: int
    username: str
    display_name: str | None
    email: str | None
    role: UserRole
    is_active: bool


class MediaUserImportRow(BaseModel):
    source_user_id: str
    username: str
    email: str | None = None
    local_username: str | None = None
    account: MediaImportAccount | None = None
    status: Literal[
        "ready_to_create",
        "ready_to_link",
        "needs_review",
        "conflict",
        "skipped",
        "created",
        "linked",
        "error",
    ]
    message: str
    revision: str = ""


class MediaUserImportResponse(BaseModel):
    rows: list[MediaUserImportRow]
    ready: int = 0
    created: int = 0
    linked: int = 0
    skipped: int = 0
    errors: int = 0
