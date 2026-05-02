from typing import Any

from pydantic import BaseModel, Field

from backend.enums import MediaType


class ValidateRegexRequest(BaseModel):
    base_path: str = ""
    suffix: str = ""


class ValidateRegexResponse(BaseModel):
    valid: bool
    error: str | None = None
    pattern: str | None = None


class ValidatePathsRequest(BaseModel):
    media_type: MediaType
    library_ids: list[str] | None = None
    paths: list[str]


class ValidatePathsResponse(BaseModel):
    valid_paths: list[str]
    invalid_paths: list[str]


class RulePreviewRequest(BaseModel):
    name: str | None = None
    media_type: MediaType
    target_scope: str
    definition: dict[str, Any]
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=25, ge=1, le=100)
