from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.enums import MediaType

CandidateScope = Literal["movie", "version", "series", "season", "episode"]
CandidateDeletionBlockerCode = Literal[
    "active_protection",
    "pending_protection_request",
    "pending_delete_request",
    "resync_pending",
    "rule_data_unavailable",
    "scheduled_deletion",
    "manual_schedule",
]


class CandidateDeletionBlocker(BaseModel):
    code: CandidateDeletionBlockerCode
    scope: CandidateScope | None = None
    target_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None


class CleanupRuleBase(BaseModel):
    """Advanced cleanup rule payload."""

    name: str
    description: str | None = None
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
    description: str | None = None
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
    # Rules that imported but need a look: a requester condition naming a Seerr
    # this install does not have would match nobody, and silence about that is
    # the failure the qualified-id format exists to prevent.
    warnings: list[str] = Field(default_factory=list)


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
    season_inventory_unavailable_count: int = 0
    season_inventory_unavailable_examples: list[str] = field(default_factory=list)
    season_inventory_unavailable_keys: set[tuple[int, int]] = field(
        default_factory=set,
        repr=False,
    )
    playback_unavailable_count: int = 0
    playback_error: str | None = None
    # Missing fields on other library targets are diagnostic warnings. A failed
    # refresh/provider is blocking. None preserves fail-closed older callers.
    playback_blocking: bool | None = None
    seerr_unavailable: bool = False
    seerr_error: str | None = None
    seerr_unavailable_instances: list[str] = field(default_factory=list)
    requester_watch_unavailable_count: int = 0
    watch_completion_unavailable_count: int = 0
    matched_count: int = 0
    # Scan reconciliation retains existing matches when their rule is unknown.
    unavailable_rule_ids_by_target: dict[tuple[str, int], set[int]] = field(
        default_factory=dict, repr=False
    )


@dataclass(slots=True)
class RulePreviewMatchResult:
    """Preview matches and metadata returned by preview collection."""

    matches: list[MatchedCandidateRecord]
    metadata: RulePreviewMatchMetadata
