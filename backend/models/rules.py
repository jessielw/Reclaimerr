from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.enums import MediaType


class ValidateRegexRequest(BaseModel):
    base_path: str = ""
    suffix: str = ""


class ValidateRegexResponse(BaseModel):
    valid: bool
    error: str | None = None
    pattern: str | None = None


class ValidatePathCondition(BaseModel):
    field: Literal["media.path", "media.file_name"]
    operator: str
    value: str


class ValidatePathsRequest(BaseModel):
    media_type: MediaType
    library_ids: list[str] | None = None
    paths: list[str] = Field(default_factory=list)
    conditions: list[ValidatePathCondition] = Field(default_factory=list)


class ValidatePathsResponse(BaseModel):
    valid_paths: list[str] = Field(default_factory=list)
    invalid_paths: list[str] = Field(default_factory=list)
    valid_conditions: list[ValidatePathCondition] = Field(default_factory=list)
    invalid_conditions: list[ValidatePathCondition] = Field(default_factory=list)


class RulePreviewRequest(BaseModel):
    name: str | None = None
    media_type: MediaType
    target_scope: str
    definition: dict[str, Any]
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=25, ge=1, le=100)


class SeerrUserLookupResponse(BaseModel):
    id: int
    username: str | None = None
    display_name: str | None = None


class MovieCollectionLookupResponse(BaseModel):
    name: str
    movie_count: int


class PaginatedMovieCollectionsResponse(BaseModel):
    items: list[MovieCollectionLookupResponse]
    total: int
    page: int
    per_page: int
    total_pages: int
