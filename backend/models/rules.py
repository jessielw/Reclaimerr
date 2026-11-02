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
    outcome: Literal["candidate", "protect", "match"] = "candidate"
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=25, ge=1, le=100)


RuleTargetScope = Literal["movie_version", "series", "season", "episode"]
RuleCheckState = Literal["pass", "fail", "unknown", "disabled"]


class RuleExplainRequest(BaseModel):
    name: str | None = None
    target_scope: RuleTargetScope
    target_id: int = Field(gt=0)
    definition: dict[str, Any]
    outcome: Literal["candidate", "protect", "match"] = "candidate"


class RuleExplainTarget(BaseModel):
    id: int
    label: str
    path: str | None = None


class RuleExplainTargetsResponse(BaseModel):
    items: list[RuleExplainTarget]
    total: int
    page: int
    per_page: int


class RuleExplainNode(BaseModel):
    type: Literal["group", "condition"]
    state: RuleCheckState
    op: str | None = None
    field: str | None = None
    label: str | None = None
    operator: str | None = None
    operator_label: str | None = None
    expected: Any = None
    actual: Any = None
    children: list["RuleExplainNode"] = Field(default_factory=list)


class RuleExplainResponse(BaseModel):
    target: RuleExplainTarget
    target_scope: RuleTargetScope
    outcome: Literal["candidate", "protect", "match"]
    condition_state: RuleCheckState
    eligible: bool
    exclusions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    root: RuleExplainNode


class QualityProfileLookupResponse(BaseModel):
    """One Arr quality profile, as offered in the rule action editor."""

    service_config_id: int
    service_name: str | None = None
    id: int
    name: str


class QualityProfileLookupError(BaseModel):
    service_config_id: int
    service_name: str | None = None
    message: str


class QualityProfileLookup(BaseModel):
    profiles: list[QualityProfileLookupResponse]
    # instances that did not answer; the profiles above are still usable
    errors: list[QualityProfileLookupError]


class SeerrUserLookupResponse(BaseModel):
    # The instance-qualified identity a rule stores. `id` beside it is what that
    # Seerr calls the user, which is what its own UI shows.
    key: str
    service_config_id: int
    service_name: str | None = None
    id: int
    username: str | None = None
    display_name: str | None = None
    # Every value matching actually tries, so a coverage report cannot claim a
    # user is unmapped over an identity the matcher would have used.
    identities: list[str] = Field(default_factory=list)


class PlaybackUserLookupResponse(BaseModel):
    # The value a rule stores; rule matching expands it to every alias below.
    username: str
    # Set only for accounts no provider could name.
    display_name: str | None = None
    source_services: list[str] = Field(default_factory=list)
    # Other keys this person's playback is recorded under.
    aliases: list[str] = Field(default_factory=list)


class MovieCollectionLookupResponse(BaseModel):
    name: str
    movie_count: int


class PaginatedMovieCollectionsResponse(BaseModel):
    items: list[MovieCollectionLookupResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class GenreLookupResponse(BaseModel):
    name: str
    media_count: int


class PaginatedGenresResponse(BaseModel):
    items: list[GenreLookupResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class MetadataValueLookupResponse(BaseModel):
    value: str
    name: str
    media_count: int


class PaginatedMetadataValuesResponse(BaseModel):
    items: list[MetadataValueLookupResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class MediaServerCollectionLookupResponse(BaseModel):
    name: str
    media_count: int


class PaginatedMediaServerCollectionsResponse(BaseModel):
    items: list[MediaServerCollectionLookupResponse]
    total: int
    page: int
    per_page: int
    total_pages: int
