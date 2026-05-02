from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from backend.enums import MediaType


class CleanupRuleBase(BaseModel):
    """Advanced cleanup rule payload."""

    name: str
    media_type: MediaType
    enabled: bool = True
    target_scope: str
    definition: dict[str, Any]
    action: dict[str, Any] | None = None


class CleanupRuleCreate(CleanupRuleBase):
    """Model for creating a new cleanup rule."""

    pass


class CleanupRuleUpdate(BaseModel):
    """Model for updating an existing cleanup rule. All fields are optional.

    This model intentionally declares optional update fields explicitly to keep
    PATCH semantics clear and stable with ``exclude_unset=True``.
    """

    name: str | None = None
    media_type: MediaType | None = None
    enabled: bool | None = None
    target_scope: str | None = None
    definition: dict[str, Any] | None = None
    action: dict[str, Any] | None = None


class CleanupRuleResponse(CleanupRuleBase):
    """Model for cleanup rule API responses."""

    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
