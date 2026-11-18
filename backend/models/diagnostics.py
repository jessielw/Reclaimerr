"""Versioned, explicitly allowlisted support exports (never ORM responses)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from backend.core.support_codes import normalize_support_code

Scope = Literal["movie", "movie_version", "series", "season", "episode"]
EvidenceState = Literal[
    "available",
    "zero",
    "unknown",
    "not_configured",
    "unavailable",
    "stale",
    "not_collected",
]


class ExportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(ExportModel):
    scope: Scope
    id: int = Field(gt=0)


class DiagnosticsOptions(ExportModel):
    include_file_paths: bool = False
    history_days: int = Field(default=7, ge=1, le=30)
    event_limit: int = Field(default=50, ge=1, le=100)


class MediaDiagnosticsOptions(DiagnosticsOptions):
    target: Target
    children_offset: int = Field(default=0, ge=0, le=1000000)


class Observation(ExportModel):
    section: str
    observed_at: datetime
    state: Literal["available", "unavailable", "not_collected"]
    reason: str | None = None


class Limit(ExportModel):
    section: str
    returned: int
    total: int | None = None
    reason: str = "sample_limit"


class Collection(ExportModel):
    started_at: datetime
    completed_at: datetime | None = None
    partial: bool = False
    budget_seconds: float = 5
    max_bytes: int = 524288
    observations: list[Observation] = Field(default_factory=list)
    truncated: list[Limit] = Field(default_factory=list)
    omitted: list[str] = Field(default_factory=list)
    children_offset: int = 0
    children_total: int | None = None


class Sharing(ExportModel):
    file_paths_included: bool
    masked_categories: list[str] = Field(
        default_factory=lambda: [
            "accounts",
            "servers",
            "libraries",
            "addresses",
            "free_text",
            "rule_text_literals",
        ]
    )
    notice: str = "Includes media titles, canonical IDs and playback information. Review before sharing. Nothing is uploaded automatically."


class AppInfo(ExportModel):
    version: str
    python: str
    platform: str
    deployment: Literal["desktop_bundle", "container", "source_or_package"]


class Refresh(ExportModel):
    state: EvidenceState = "not_collected"
    available: bool | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_event_at: datetime | None = None
    imported_events: int | None = None
    format_version: int | None = None
    aggregate_format_version: int | None = None
    error: Literal["provider_reported_error"] | None = None


class ServiceInfo(ExportModel):
    id: int
    alias: str
    service: str
    enabled: bool
    is_main: bool
    last_synced_at: datetime | None = None
    selected_provider: str | None = None
    selection_preference: (
        Literal["automatic", "tautulli", "tracearr", "playback_reporting", "unknown"]
        | None
    ) = None
    provider_config_id: int | None = None
    remote_server: str | None = None
    selection_state: EvidenceState = "not_configured"
    native_refresh: Refresh = Field(default_factory=Refresh)
    history_refresh: Refresh = Field(default_factory=Refresh)


class LibraryInfo(ExportModel):
    id: int
    alias: str
    source_id: str
    server_id: int | None = None
    selected: bool
    media_type: str


class PathMapping(ExportModel):
    source: str | None
    destination: str | None
    service: str | None = None
    server_id: int | None = None


class RequesterWatchMapping(ExportModel):
    seerr_service_config_id: int | None = None
    seerr_user_id: str | None = None
    seerr_username: str | None = None
    media_user_key: str | None = None
    service_type: str | None = None


class Configuration(ExportModel):
    path_mappings: list[PathMapping] = Field(default_factory=list)
    move_destination_movies: str | None = None
    move_destination_series: str | None = None
    active_playback_protection_enabled: bool
    playback_unavailable_protection_enabled: bool
    media_server_fallback_enabled: bool
    auto_delete_movie_delay_days: int
    auto_delete_series_delay_days: int
    playback_movie_min_seconds: int
    playback_episode_min_seconds: int
    default_arr_delete_behavior: Literal[
        "delete", "unmonitor", "unmonitor_only", "remove_if_empty", "unknown"
    ]
    add_arr_import_exclusions_on_delete: bool
    favorites_ignore_enabled: bool
    favorites_protect_all_users: bool
    favorites_usernames: list[str] = Field(default_factory=list)
    requester_watch_ignore_request_date: bool
    requester_watch_user_mappings: list[RequesterWatchMapping] = Field(
        default_factory=list
    )


class DatabaseInfo(ExportModel):
    sqlite_version: str
    migration_revisions: list[str]
    journal_mode: str
    query_only: int
    busy_timeout: int
    foreign_keys: int
    synchronous: int
    database_bytes: int | None = None
    wal_bytes: int | None = None
    connection_scope: str = "Dedicated read-only diagnostics connection; PRAGMAs do not describe application pool connections."


class ScheduleInfo(ExportModel):
    id: int
    task: str
    enabled: bool
    schedule_type: str
    schedule_value: str


class RunInfo(ExportModel):
    id: int
    task: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    items_processed: int | None = None
    error: Literal["recorded_error_text_omitted"] | None = None


class PlaybackSourceObservation(ExportModel):
    kind: Literal["history", "native"]
    config_id: int | None = None
    provider: str
    observed_service: str | None = None
    observed_config_id: int | None = None
    available: bool | None = None
    error_category: str | None = None


class PlaybackCoverageObservation(ExportModel):
    observed_at: datetime
    require_fresh: bool | None = None
    blocking: bool | None = None
    library_unavailable_count: int | None = None
    selected_unavailable_count: int | None = None
    error_category: str | None = None
    native_refresh_error_category: str | None = None
    sources: list[PlaybackSourceObservation] = Field(default_factory=list)
    unknown_target_samples: list[Target] = Field(default_factory=list)
    sources_truncated: bool = False
    targets_truncated: bool = False
    note: str = "Captured during revalidation, not refreshed during export. Library counts and samples include unrelated media; selected counts describe the revalidation batch."


class PlaybackFieldObservation(ExportModel):
    requested: list[str] = Field(default_factory=list)
    available: list[str] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)


class AutoDeleteRunInfo(ExportModel):
    correlation_id: str
    started_at: datetime
    completed_at: datetime | None = None
    job_id: int | None = None
    status: Literal["completed", "failed", "interrupted", "unknown"]
    summary: dict[str, int] | None = None
    error_category: str | None = None
    dropped_decisions: int = 0
    dropped_traces: int = 0
    revalidation_phase: str | None = None
    playback: PlaybackCoverageObservation | None = None


class JobInfo(ExportModel):
    id: int
    job_type: str
    status: str
    attempts: int
    created_at: datetime | None
    scheduled_at: datetime | None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    queue_age_seconds: float | None = None
    error: Literal["recorded_error_text_omitted"] | None = None
    error_category: str | None = None
    task: str | None = None
    trigger: Literal["manual", "scheduled", "system"] | None = None
    service_config_id: int | None = None
    candidate_ids: list[int] = Field(default_factory=list)
    candidate_ids_truncated: bool = False
    targets: list[Target] = Field(default_factory=list)
    targets_truncated: bool = False
    progress: dict[str, int] | None = None
    result: dict[str, int] | None = None
    scope: Literal["app_wide"] = "app_wide"
    note: str = "Stored job context; task totals do not establish an individual target's outcome. Missing counters are unknown."


class Identity(ExportModel):
    id: int
    scope: Scope
    title: str | None = None
    parent_title: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    movie_id: int | None = None
    series_id: int | None = None
    season_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    removed_at: datetime | None = None
    size: int | None = None
    video_width: int | None = None
    video_height: int | None = None
    path: str | None = None
    view_count: int | None = None
    last_viewed_at: datetime | None = None
    updated_at: datetime | None = None


class StorageIdentity(ExportModel):
    id: int
    kind: Literal[
        "movie_version", "episode_version", "series_ref", "linked_match", "arr_ref"
    ]
    service: str | None = None
    server_id: int | None = None
    item: str | None = None
    library: str | None = None
    library_source_id: str | None = None
    path: str | None = None
    mapped_paths: list[str] = Field(default_factory=list)
    size: int | None = None
    updated_at: datetime | None = None
    reachability: Literal["not_collected"] = "not_collected"


class Aggregate(ExportModel):
    kind: Literal["native", "imported"]
    target: Target
    server_id: int | None = None
    state: EvidenceState
    play_count: int
    unique_user_count: int
    last_activity_at: datetime | None = None
    refreshed_at: datetime | None = None
    total_duration_seconds: int | None = None
    has_activity: bool | None = None
    usernames_complete: bool | None = None
    unavailable_fields: dict[
        str, Literal["activity_date_unknown", "incomplete_usernames"]
    ] = Field(default_factory=dict)
    note: str


class EventInfo(ExportModel):
    id: int
    provider: str
    provider_config_id: int
    observed_server_id: int | None = None
    item: str
    user: str | None = None
    username: str | None = None
    played_at: datetime | None
    duration_seconds: int
    completed: bool | None = None
    selected_source: bool | None = None
    association: Literal["stored_target_link", "canonical_id_only"]
    identity_conflict: bool | None = None
    identity_unverified: bool | None = None


class Playback(ExportModel):
    native_state: EvidenceState = "unknown"
    imported_state: EvidenceState = "unknown"
    note: str = "Stored aggregates are not recomputed for the selected provider. Missing rows and unmatched history are unknown, not zero. Event samples are evidence, not a complete rule evaluation."
    aggregates: list[Aggregate] = Field(default_factory=list)
    events: list[EventInfo] = Field(default_factory=list)
    stored_event_count: int | None = None
    unmatched_event_count: int | None = None
    coverage_note: str = "Counts describe persisted foreign-key matches, not a new identity reconciliation."


class Condition(ExportModel):
    rule_id: int | None = None
    field: str
    field_label: str = ""
    operator_label: str = ""
    operator: str | None = None
    expected: JsonValue = None
    actual: JsonValue = None


DeferralCode = Literal[
    "currently_playing",
    "playback_unavailable",
    "resync_pending",
    "manual_deletion_scheduled",
    "unknown",
]


class DeferralInfo(ExportModel):
    code: DeferralCode
    checked_at: datetime | None = None
    server_config_ids: list[int] = Field(default_factory=list)
    completed_step_count: int | None = None


class CandidateInfo(ExportModel):
    id: int
    target: Target
    matched_rule_ids: list[int]
    recorded_at: datetime | None = None
    last_updated_at: datetime | None = None
    reviewed: bool
    approved_for_deletion: bool
    delete_attempts: int
    last_delete_attempt_at: datetime | None = None
    delete_error: Literal["recorded_error_text_omitted"] | None = None
    conditions: list[Condition] = Field(default_factory=list)
    conditions_state: Literal["recorded", "not_collected"] = "not_collected"
    auto_delete_cancelled_at: datetime | None = None
    auto_delete_postponed_until: datetime | None = None
    auto_delete_timer_started_at: datetime | None = None
    delete_request_id: int | None = None
    playback_deferral: DeferralInfo | None = None
    unavailable_rule_ids: list[int] = Field(default_factory=list)
    resync_pending: bool | None = None
    resync_path: str | None = None


class RuleAction(ExportModel):
    outcome: Literal["candidate", "protect", "match"] | None = None
    auto_delete_enabled: bool | None = None
    auto_delete_delay_days: int | None = None
    media_server_action: Literal["delete", "none"] | None = None
    arr_action: (
        Literal[
            "delete",
            "unmonitor",
            "unmonitor_only",
            "unmonitor_delete_monitor_new_seasons",
            "unmonitor_delete_list_exclusion",
            "unmonitor_delete_monitor_or_exclude",
            "change_quality_profile",
        ]
        | None
    ) = None
    move_instead_of_delete: bool | None = None
    radarr_service_config_ids: list[int] = Field(default_factory=list)
    sonarr_service_config_ids: list[int] = Field(default_factory=list)
    quality_profile_id: int | None = None
    trigger_search: bool | None = None
    candidate: bool | None = None
    tag_enabled: bool | None = None
    arr_tag: str | None = None


class RuleInfo(ExportModel):
    id: int
    alias: str
    enabled: bool
    target_scope: str | None = None
    definition: JsonValue = None
    action: RuleAction | None = None
    note: str = "Current saved definition; not necessarily the definition used for the recorded match."


class ProtectionInfo(ExportModel):
    id: int
    target: Target
    permanent: bool
    expires_at: datetime | None = None
    source_rule_id: int | None = None
    relationship: Literal["selected", "ancestor", "descendant", "sibling"] = "selected"
    active: bool = False


class ReviewInfo(ExportModel):
    id: int
    target: Target
    status: str
    reviewed_at: datetime | None = None
    executed_at: datetime | None = None
    error: Literal["recorded_error_text_omitted"] | None = None
    relationship: Literal["selected", "ancestor", "descendant", "sibling"] = "selected"
    playback_deferral: DeferralInfo | None = None
    resync_pending: bool | None = None
    resync_path: str | None = None


class ScheduledDeletionInfo(ExportModel):
    id: int
    media_target: Target
    target: Target | None = None
    scope: Literal["movie", "version", "series", "season", "episode"]
    scheduled_at: datetime | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    state: Literal["scheduled", "running", "blocked", "failed", "unknown"]
    job_id: int | None = None
    actor: str | None = None
    error_category: str | None = None
    holds_selected_scope: bool = False
    stored_path_overlap: bool = False
    note: str = "Active manual deletion intent from stored state. Scope/path overlap holds cleanup until resolved; same-parent context alone does not establish a hold. No inventory or permissions revalidated."


class Decisions(ExportModel):
    basis: str = "Recorded state only. Candidate creation/update times are not a rule execution trace; exact scan time and missing prerequisites were not persisted. Parent and child scopes are explicit."
    candidates: list[CandidateInfo] = Field(default_factory=list)
    rules: list[RuleInfo] = Field(default_factory=list)
    protections: list[ProtectionInfo] = Field(default_factory=list)
    deletion_requests: list[ReviewInfo] = Field(default_factory=list)
    protection_requests: list[ReviewInfo] = Field(default_factory=list)
    scheduled_deletions: list[ScheduledDeletionInfo] = Field(default_factory=list)


class Sections(ExportModel):
    database: DatabaseInfo | None = None
    services: list[ServiceInfo] | None = None
    libraries: list[LibraryInfo] | None = None
    configuration: Configuration | None = None
    schedules: list[ScheduleInfo] | None = None
    recent_runs: list[RunInfo] | None = None
    auto_delete_runs: list[AutoDeleteRunInfo] | None = None
    active_jobs: list[JobInfo] | None = None
    recent_jobs: list[JobInfo] | None = None
    identity: Identity | None = None
    children: list[Identity] | None = None
    storage: list[StorageIdentity] | None = None
    playback: Playback | None = None
    decisions: Decisions | None = None


class DiagnosticsReport(ExportModel):
    schema_version: Literal[1] = 1
    report_kind: Literal["media", "system"]
    report_id: str
    generated_at: datetime
    app: AppInfo
    target: Target | None = None
    summary: list[str] = Field(default_factory=list)
    sections: Sections = Field(default_factory=Sections)
    collection: Collection
    sharing: Sharing


BundleSection = Literal[
    "services", "configuration", "storage", "playback", "decisions", "tasks", "timeline"
]
BundlePreset = Literal["playback", "candidate", "deletion", "paths", "system"]


class BundleOptions(DiagnosticsOptions):
    target: Target | None = None
    preset: BundlePreset = "system"
    sections: list[BundleSection] = Field(
        default_factory=lambda: ["services", "configuration", "tasks", "timeline"],
        max_length=7,
    )
    include_sqlite: bool = False
    include_logs: bool = False
    support_code: str | None = Field(default=None, max_length=80)
    check_ids: list[UUID] = Field(default_factory=list, max_length=5)

    @field_validator("support_code")
    @classmethod
    def valid_code(cls, value):
        return normalize_support_code(value) if value is not None else None

    @model_validator(mode="before")
    @classmethod
    def preset_sections(cls, value):
        if isinstance(value, dict) and "sections" not in value:
            defaults = {
                "playback": ["services", "configuration", "playback", "timeline"],
                "candidate": [
                    "services",
                    "configuration",
                    "storage",
                    "playback",
                    "decisions",
                    "tasks",
                    "timeline",
                ],
                "deletion": [
                    "services",
                    "configuration",
                    "storage",
                    "playback",
                    "decisions",
                    "tasks",
                    "timeline",
                ],
                "paths": ["services", "configuration", "storage"],
                "system": ["services", "configuration", "tasks", "timeline"],
            }
            preset = value.get("preset", "system")
            if isinstance(preset, str) and preset in defaults:
                return {**value, "sections": defaults[preset]}
        return value


class TraceCondition(Condition):
    availability: Literal["available", "unknown", "not_evaluated"]
    outcome: Literal["matched", "not_matched", "unknown", "not_evaluated"] = "unknown"


class DecisionTrace(ExportModel):
    id: int
    scan_id: str
    job_id: int | None = None
    target: Target
    candidate_id: int | None = None
    rule_id: int
    rule_revision: str
    observed_at: datetime
    outcome: Literal["matched", "not_matched", "unknown"]
    definition: JsonValue
    conditions: list[TraceCondition]
    protection_checks: JsonValue
    scan_context: JsonValue = None
    note: str = "Captured during this evaluation without rerunning it. Disabled and short-circuited conditions are not evaluated. Values and definitions are bounded; SQL-filtered targets may have no trace."


class OperationDetail(ExportModel):
    kind: Literal["candidate", "duplicate", "upgrade_leftover"] = "candidate"
    action: Literal["move", "delete", "unknown"]
    stage: str
    outcome: Literal["succeeded", "failed", "deferred", "skipped", "unknown"]
    reason: Literal["favorites_protection", "sibling_protection"] | None = None
    subject: Literal["file", "directory", "path", "candidate", "provider_request"]
    path: str | None = None
    destination: str | None = None
    provider: str | None = None
    reference: str | None = None
    shared_request: bool = False
    dropped_observations: int = 0
    deferral_code: str | None = None


class AutoDeleteDecisionDetail(ExportModel):
    stage: Literal["selection", "revalidation", "dispatch"]
    outcome: Literal["eligible", "waiting", "skipped", "authorized"]
    reason: str
    rule_ids: list[int] = Field(default_factory=list)
    matched_rule_ids: list[int] = Field(default_factory=list)
    unavailable_rule_ids: list[int] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    eligible_at: datetime | None = None
    coverage: dict[str, int | bool] = Field(default_factory=dict)
    playback_fields: PlaybackFieldObservation | None = None
    note: str = "Observed during automatic deletion. Authorization is not proof of deletion. Coverage counters describe the revalidation batch; condition traces describe individual rule inputs."


class TimelineEntry(ExportModel):
    occurred_at: datetime
    event: str
    correlation_id: str
    target: Target | None = None
    candidate_id: int | None = None
    job_id: int | None = None
    error_category: str | None = None
    note: str | None = None
    operation: OperationDetail | None = None
    auto_delete: AutoDeleteDecisionDetail | None = None
    job: JobInfo | None = None


class LogExcerpt(ExportModel):
    timestamp: str
    level: str
    category: str
    message: str
    correlation_id: str | None = None


class BundleInventory(ExportModel):
    section: str
    records: int


class BundleManifest(ExportModel):
    format_version: Literal[1] = 1
    report_id: str
    preset: BundlePreset
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    inventory: list[BundleInventory]
    files: list[str]
    excluded: list[str]
    limitations: list[str]
    file_paths_included: bool
    restorable: Literal[False] = False
    support_code: str | None = None


class SupportLookupOptions(ExportModel):
    code: str = Field(min_length=1, max_length=80)

    @field_validator("code")
    @classmethod
    def valid_code(cls, value):
        return normalize_support_code(value)


class SupportMatch(ExportModel):
    code: str
    kind: Literal["scan", "operation", "job"]
    occurred_at: datetime | None = None
    targets: list[Target] = Field(default_factory=list)
    targets_truncated: bool = False
    suggested: BundleOptions


class SupportLookupResult(ExportModel):
    state: Literal["found", "not_found", "unavailable"]
    matches: list[SupportMatch] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class BundlePreview(ExportModel):
    download_token: str
    expires_at: datetime
    archive_bytes: int
    sha256: str
    manifest: BundleManifest
    report: DiagnosticsReport
    traces: list[DecisionTrace]
    timeline: list[TimelineEntry]
    logs: list[LogExcerpt]
    checks: list["DiagnosticCheckResult"] = Field(default_factory=list)


class DiagnosticCheckRequest(DiagnosticsOptions):
    kind: Literal["path", "provider", "relationships"]
    target: Target | None = None
    service_config_id: int | None = Field(default=None, gt=0)


class CheckObservation(ExportModel):
    subject: str
    status: Literal["passed", "failed", "unknown"]
    detail: str


class DiagnosticCheckResult(ExportModel):
    check_id: str
    kind: Literal["path", "provider", "relationships"]
    checked_at: datetime
    observations: list[CheckObservation]
    target: Target | None = None
    service_config_id: int | None = None
    file_paths_included: bool = False
    attachment_expires_at: datetime | None = None
    note: str = "Explicit check only; no repairs, provider syncs or cleanup were run. Aliases are local to this check and may differ from report aliases."
