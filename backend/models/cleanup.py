from __future__ import annotations

from dataclasses import dataclass
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


class RuleImportPayload(BaseModel):
    """Payload for bulk rule import."""

    rules: list[CleanupRuleCreate]


class RuleImportResponse(BaseModel):
    """Response for bulk rule import."""

    imported: int
    errors: list[str]


@dataclass(slots=True)
class MatchedCandidateRecord:
    """Represents a record of a media item that matched a cleanup rule."""

    media_type: MediaType
    matched_rule_ids: list[int]
    matched_criteria: dict[str, Any]
    reason: str
    reason_data: list[dict[str, Any]]
    estimated_space_bytes: int | None
    movie_id: int | None = None
    movie_version_id: int | None = None
    series_id: int | None = None
    season_id: int | None = None
    episode_id: int | None = None


@dataclass(slots=True)
class RulePreviewMatchMetadata:
    """Aggregate counters captured during preview collection."""

    source_media_count: int = 0
    skipped_favorites_count: int = 0
    skipped_protected_count: int = 0
    sonarr_unavailable_count: int = 0
    sonarr_error: str | None = None
    matched_count: int = 0


@dataclass(slots=True)
class RulePreviewMatchResult:
    """Preview matches and metadata returned by preview collection."""

    matches: list[MatchedCandidateRecord]
    metadata: RulePreviewMatchMetadata
