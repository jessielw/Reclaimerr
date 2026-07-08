from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field

from backend.enums import MediaType
from backend.user_types import AudioCodecFamily, MediaServerType, VideoCodecFamily


@dataclass(slots=True, frozen=True)
class ExternalIDs:
    """External provider IDs for media items."""

    tmdb: int
    imdb: str | None
    tmdb_collection: str | None
    tvdb: str | None


@dataclass(slots=True, frozen=True)
class MovieVersionData:
    """Single physical file version of a movie."""

    service: MediaServerType
    # plex ratingKey or jellyfin/emby item ID (used for item level ops like delete)
    service_item_id: str
    # plex Media.id or jellyfin/emby MediaSource.Id (unique per physical file)
    service_media_id: str
    library_id: str
    library_name: str
    path: str | None
    size: int
    added_at: datetime | None

    # metadata
    file_name: str | None = None
    container: str | None = None
    # ms (plex is milliseconds, jellyfin/emby is .net ticks so 'milliseconds = RunTimeTicks / 10,000')
    duration: float | None = None

    # video
    video_track_count: int | None = None
    # preserve raw value from provider
    video_codec: str | None = None
    # stable normalized grouping for filtering/UI logic
    video_codec_family: VideoCodecFamily | None = None
    video_hdr: bool | None = None
    video_dolby_vision: bool | None = None
    video_dolby_vision_profile: str | None = None
    video_bitrate: int | None = None
    video_bit_depth: int | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_resolution: str | None = None
    video_color_primaries: str | None = None
    video_color_space: str | None = None
    video_color_transfer: str | None = None
    video_fps: float | None = None

    # audio
    audio_count: int | None = None
    audio_languages: list[str] | None = None
    # preserve raw value from provider
    audio_codec: str | None = None
    # stable normalized grouping for filtering/UI logic
    audio_codec_family: AudioCodecFamily | None = None
    audio_title: str | None = None
    audio_language: str | None = None
    audio_channels: int | None = None
    audio_channel_layout: str | None = None
    audio_bitrate: int | None = None
    audio_sample_rate: int | None = None

    # subtitles
    subtitle_count: int | None = None
    subtitle_has_forced: bool | None = None
    subtitle_languages: list[str] | None = None

    # chapters
    has_chapters: bool | None = None
    media_server_collection_names: list[str] | None = None


@dataclass(slots=True, frozen=True)
class AggregatedMovieData:
    """Movie with aggregated watch data across all users, plus all physical file versions."""

    name: str
    year: int | None
    external_ids: ExternalIDs
    versions: list[MovieVersionData]
    # watch data
    view_count: int
    last_viewed_at: datetime | None
    # jellyfin-specific (None for Plex)
    played_by_user_count: int | None = None


@dataclass(slots=True, frozen=True)
class AggregatedEpisodeData:
    """Per episode data collected from a media server during sync."""

    episode_number: int
    view_count: int
    name: str | None = None
    air_date: datetime | None = None
    last_viewed_at: datetime | None = None
    size: int | None = None
    path: str | None = None
    plex_rating_key: str | None = None
    jellyfin_episode_id: str | None = None
    emby_episode_id: str | None = None


@dataclass(slots=True, frozen=True)
class MediaWatchSnapshot:
    """Latest per-user watch state for a movie or individual TV episode."""

    media_type: MediaType
    tmdb_id: int
    watch_user_key: str
    last_watched_at: datetime
    play_count: int | None = None
    source_item_id: str | None = None


@dataclass(slots=True, frozen=True)
class AggregatedSeasonData:
    """Season with aggregated watch data from a media server."""

    # (service_series_id, season_number) uniquely identifies this season
    service_series_id: str  # plex grandparentRatingKey or jellyfin/emby SeriesId
    season_number: int
    size: int
    episode_count: int
    view_count: int
    last_viewed_at: datetime | None
    added_at: datetime | None = None
    air_date: datetime | None = None
    # plex parentRatingKey or jellyfin/emby season item ID for direct ops
    service_season_id: str | None = None
    # filesystem path to the season folder (derived from episode paths during sync)
    path: str | None = None
    # all episode file paths belonging to this season (as reported by the media server)
    episode_paths: list[str] | None = None
    # per-episode data (populated by service layer when available)
    episode_data: list[AggregatedEpisodeData] = field(default_factory=list)
    # aggregate media signals
    has_hdr: bool | None = None
    has_dolby_vision: bool | None = None
    max_video_width: int | None = None
    max_video_height: int | None = None
    video_codec_families: list[str] | None = None
    audio_codec_families: list[str] | None = None
    audio_languages: list[str] | None = None
    max_audio_channels: int | None = None
    subtitle_languages: list[str] | None = None


@dataclass(slots=True, frozen=True)
class AggregatedSeriesData:
    """Series with aggregated watch data across all users."""

    id: str
    name: str
    year: int | None
    service: MediaServerType
    library_name: str
    library_id: str
    path: str | None
    added_at: datetime | None
    external_ids: ExternalIDs
    size: int
    # watch data
    view_count: int
    last_viewed_at: datetime | None
    # jellyfin/emby specific (None for Plex)
    played_by_user_count: int | None = None
    media_server_collection_names: list[str] | None = None
    # season level breakdown (populated by service layer)
    season_data: list[AggregatedSeasonData] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ArrTag:
    id: int
    label: str


class MovieVersionResponse(BaseModel):
    """API representation of a single physical file version."""

    id: int
    service: str
    service_item_id: str
    service_media_id: str
    library_id: str
    library_name: str
    path: str | None
    size: int
    added_at: str | None
    arr_added_at: str | None
    file_name: str | None
    container: str | None
    duration: float | None

    # video
    video_track_count: int | None
    video_codec: str | None
    video_codec_family: VideoCodecFamily | None
    video_hdr: bool | None
    video_dolby_vision: bool | None
    video_dolby_vision_profile: str | None
    video_bitrate: int | None
    video_bit_depth: int | None
    video_width: int | None
    video_height: int | None
    video_resolution: str | None
    video_color_primaries: str | None
    video_color_space: str | None
    video_color_transfer: str | None
    video_fps: float | None

    # audio
    audio_count: int | None
    audio_languages: list[str] | None
    audio_codec: str | None
    audio_codec_family: AudioCodecFamily | None
    audio_title: str | None
    audio_language: str | None
    audio_channels: int | None
    audio_channel_layout: str | None
    audio_bitrate: int | None
    audio_sample_rate: int | None

    # subtitles/chapters
    subtitle_count: int | None
    subtitle_has_forced: bool | None
    subtitle_languages: list[str] | None
    has_chapters: bool | None


class MediaStatusInfo(BaseModel):
    """Status information for a media item."""

    is_candidate: bool = False
    candidate_id: int | None = None
    candidate_reason: str | None = None
    candidate_space_bytes: int | None = None

    is_protected: bool = False
    protected_reason: str | None = None
    protected_permanent: bool = True

    has_pending_request: bool = False
    request_id: int | None = None
    request_status: str | None = None
    request_reason: str | None = None

    has_pending_delete_request: bool = False
    delete_request_id: int | None = None
    delete_request_status: str | None = None
    delete_request_reason: str | None = None


class ArrRefResponse(BaseModel):
    """API representation of an Arr instance reference."""

    service_type: str
    service_config_id: int
    arr_id: int


class MovieWithStatus(BaseModel):
    """Movie with all metadata and status information."""

    # basic info
    id: int
    title: str
    year: int | None
    tmdb_id: int

    # file info
    size: int | None
    versions: list[MovieVersionResponse]

    # arr instance refs
    arr_refs: list[ArrRefResponse]
    imdb_id: str | None
    imdb_rating: float | None = None
    imdb_vote_count: int | None = None
    imdb_ratings_refreshed_at: str | None = None
    anilist_id: int | None = None
    anilist_score: int | None = None
    anilist_popularity: int | None = None
    anilist_favourites: int | None = None
    anilist_refreshed_at: str | None = None
    rottentomatoes_tomato_meter: int | None = None
    rottentomatoes_tomato_vote_count: int | None = None
    rottentomatoes_popcorn_meter: int | None = None
    rottentomatoes_popcorn_vote_count: int | None = None
    metacritic_metascore: int | None = None
    metacritic_vote_count: int | None = None
    metacritic_user_score: int | None = None
    metacritic_user_vote_count: int | None = None
    trakt_rating: int | None = None
    trakt_vote_count: int | None = None
    letterboxd_score: int | None = None
    letterboxd_vote_count: int | None = None
    external_ratings_source: str | None = None
    external_ratings_refreshed_at: str | None = None

    # TMDB metadata
    tmdb_title: str | None
    original_title: str | None
    tmdb_release_date: str | None
    tmdb_collection_id: int | None
    tmdb_collection_name: str | None
    tmdb_in_collection: bool | None
    original_language: str | None
    poster_url: str | None
    backdrop_url: str | None
    overview: str | None
    genres: list[str] | None
    popularity: float | None
    vote_average: float | None
    vote_count: int | None
    runtime: int | None
    tagline: str | None

    # watch tracking
    last_viewed_at: str | None
    view_count: int

    # status
    status: MediaStatusInfo

    # timestamps
    added_at: str | None
    arr_added_at: str | None


class SeriesServiceRefResponse(BaseModel):
    """API representation of a service-specific reference for a series."""

    service: str
    service_id: str
    library_id: str
    library_name: str
    path: str | None


class SeriesWithStatus(BaseModel):
    """Series with all metadata and status information."""

    # basic info
    id: int
    title: str
    year: int | None
    tmdb_id: int

    # file info
    size: int | None
    service_refs: list[SeriesServiceRefResponse]

    # arr instance refs
    arr_refs: list[ArrRefResponse]
    imdb_id: str | None
    imdb_rating: float | None = None
    imdb_vote_count: int | None = None
    imdb_ratings_refreshed_at: str | None = None
    anilist_id: int | None = None
    anilist_score: int | None = None
    anilist_popularity: int | None = None
    anilist_favourites: int | None = None
    anilist_refreshed_at: str | None = None
    rottentomatoes_tomato_meter: int | None = None
    rottentomatoes_tomato_vote_count: int | None = None
    rottentomatoes_popcorn_meter: int | None = None
    rottentomatoes_popcorn_vote_count: int | None = None
    metacritic_metascore: int | None = None
    metacritic_vote_count: int | None = None
    metacritic_user_score: int | None = None
    metacritic_user_vote_count: int | None = None
    trakt_rating: int | None = None
    trakt_vote_count: int | None = None
    letterboxd_score: int | None = None
    letterboxd_vote_count: int | None = None
    external_ratings_source: str | None = None
    external_ratings_refreshed_at: str | None = None
    tvdb_id: str | None

    # TMDB metadata
    tmdb_title: str | None
    original_title: str | None
    tmdb_first_air_date: str | None
    tmdb_last_air_date: str | None
    original_language: str | None
    poster_url: str | None
    backdrop_url: str | None
    overview: str | None
    genres: list[str] | None
    popularity: float | None
    vote_average: float | None
    vote_count: int | None
    season_count: int | None
    tagline: str | None

    # watch tracking
    last_viewed_at: str | None
    view_count: int
    # aggregate media signals
    has_hdr: bool | None = None
    has_dolby_vision: bool | None = None
    max_video_width: int | None = None
    max_video_height: int | None = None
    video_codec_families: list[str] | None = None
    audio_codec_families: list[str] | None = None
    max_audio_channels: int | None = None
    subtitle_languages: list[str] | None = None

    # status
    status: MediaStatusInfo
    # true when at least one season (but not the whole series) is a reclaim candidate
    has_season_candidates: bool = False
    # number of seasons actually present in the library
    library_season_count: int = 0
    # number of episodes actually present in the library
    library_episode_count: int = 0

    # timestamps
    added_at: str | None
    arr_added_at: str | None


class SeasonWithStatus(BaseModel):
    """Season with its reclaim / protection status."""

    id: int
    season_number: int
    episode_count: int | None
    size: int | None
    view_count: int
    added_at: str | None
    arr_added_at: str | None
    last_viewed_at: str | None
    air_date: str | None
    # aggregate media signals
    has_hdr: bool | None = None
    has_dolby_vision: bool | None = None
    max_video_width: int | None = None
    max_video_height: int | None = None
    video_codec_families: list[str] | None = None
    audio_codec_families: list[str] | None = None
    audio_languages: list[str] | None = None
    max_audio_channels: int | None = None
    subtitle_languages: list[str] | None = None
    status: MediaStatusInfo


class EpisodeWithStatus(BaseModel):
    """Episode with its reclaim / protection status."""

    id: int
    season_id: int
    season_number: int
    episode_number: int
    name: str | None
    size: int | None
    view_count: int
    air_date: str | None
    arr_added_at: str | None
    last_viewed_at: str | None
    status: MediaStatusInfo


class PaginatedMediaResponse(BaseModel):
    """Paginated response wrapper."""

    items: list[MovieWithStatus | SeriesWithStatus]
    total: int
    page: int
    per_page: int
    total_pages: int


@dataclass(slots=True)
class CandidateDisplayGroup:
    group_kind: str
    media_id: int | None
    sort_title: str
    sort_created_at: datetime
    sort_deletion_at: datetime
    sort_size: int
    candidate_ids: list[int]


class CandidateLibraryRef(BaseModel):
    library_id: str
    library_name: str
    service: str | None = None


class CandidateReasonCondition(BaseModel):
    field: str
    field_label: str
    operator: str
    operator_label: str
    expected: str | int | float | bool | list[str | int | float | bool] | None = None
    actual: str | int | float | bool | list[str | int | float | bool] | None = None
    display: str


class CandidateReasonPart(BaseModel):
    rule_id: int | None = None
    rule_name: str
    target_scope: str
    season_label: str | None = None
    conditions: list[CandidateReasonCondition]
    text: str


class CandidateEntryBase(BaseModel):
    """Shared media payload used by candidate and preview responses."""

    media_type: str
    media_id: int
    media_title: str
    media_year: int | None
    poster_url: str | None
    tmdb_id: int | None = None
    tmdb_collection_id: int | None = None
    tmdb_collection_name: str | None = None
    tmdb_in_collection: bool | None = None
    imdb_id: str | None = None
    imdb_rating: float | None = None
    imdb_vote_count: int | None = None
    anilist_id: int | None = None
    anilist_score: int | None = None
    anilist_popularity: int | None = None
    anilist_favourites: int | None = None
    rottentomatoes_tomato_meter: int | None = None
    rottentomatoes_tomato_vote_count: int | None = None
    rottentomatoes_popcorn_meter: int | None = None
    rottentomatoes_popcorn_vote_count: int | None = None
    metacritic_metascore: int | None = None
    metacritic_vote_count: int | None = None
    metacritic_user_score: int | None = None
    metacritic_user_vote_count: int | None = None
    trakt_rating: int | None = None
    trakt_vote_count: int | None = None
    letterboxd_score: int | None = None
    letterboxd_vote_count: int | None = None
    external_ratings_source: str | None = None
    external_ratings_refreshed_at: str | None = None
    genres: list[str] | None = None
    popularity: float | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    tmdb_status: str | None = None
    media_library_names: list[str] | None = None
    media_added_at: str | None = None
    media_arr_added_at: str | None = None
    media_last_viewed_at: str | None = None
    media_view_count: int | None = None
    movie_version_id: int | None = None
    version_service: str | None = None
    version_library_id: str | None = None
    version_library_name: str | None = None
    version_video_codec_family: str | None = None
    version_audio_codec_family: str | None = None
    version_video_width: int | None = None
    version_video_height: int | None = None
    version_video_resolution: str | None = None
    version_video_hdr: bool | None = None
    version_video_dolby_vision: bool | None = None
    version_audio_channels: int | None = None
    version_audio_languages: list[str] | None = None
    version_size: int | None = None
    version_path: str | None = None
    version_file_name: str | None = None
    version_subtitle_languages: list[str] | None = None
    reason_parts: Sequence[CandidateReasonPart]
    reason_tokens: list[str]
    estimated_space_bytes: int | None = None
    # set for season level candidates
    season_id: int | None = None
    season_number: int | None = None
    # parent series title when candidate is season level
    series_title: str | None = None
    season_has_hdr: bool | None = None
    season_has_dolby_vision: bool | None = None
    season_max_video_width: int | None = None
    season_max_video_height: int | None = None
    season_video_codec_families: list[str] | None = None
    season_audio_codec_families: list[str] | None = None
    season_audio_languages: list[str] | None = None
    season_subtitle_languages: list[str] | None = None
    # set for episode level candidates
    episode_id: int | None = None
    episode_number: int | None = None
    episode_name: str | None = None
    series_library_refs: list[CandidateLibraryRef] | None = None


class CandidateEntry(CandidateEntryBase):
    """A single reclaim candidate with enough info to display and act on."""

    id: int
    has_pending_request: bool
    created_at: str
    auto_delete_delay_days: int
    auto_delete_eligible_at: str
    auto_delete_is_eligible: bool
    auto_delete_is_active: bool


class RulePreviewEntry(CandidateEntryBase):
    """Transient dry-run preview row for an unsaved rule."""


class RulePreviewMetadata(BaseModel):
    source_media_count: int = 0
    skipped_favorites_count: int = 0
    skipped_protected_count: int = 0
    sonarr_unavailable_count: int = 0
    sonarr_error: str | None = None
    season_inventory_unavailable_count: int = 0
    season_inventory_unavailable_examples: list[str] = Field(default_factory=list)
    playback_unavailable_count: int = 0
    playback_error: str | None = None
    matched_count: int = 0


class PaginatedCandidatesResponse(BaseModel):
    items: list[CandidateEntry]
    total: int
    page: int
    per_page: int
    total_pages: int


class CandidatesPresenceResponse(BaseModel):
    has_candidates: bool


class DeleteCandidatesRequest(BaseModel):
    candidate_ids: list[int]


class DeleteCandidatesResponse(BaseModel):
    deleted: int
    failed: int


class CandidateOperationQueuedResponse(BaseModel):
    job_id: int | None = None
    status: str
    message: str


class MoveCandidatesRequest(BaseModel):
    candidate_ids: list[int]


class MoveCandidatesResponse(BaseModel):
    moved: int
    failed: int


class PaginatedRulePreviewResponse(BaseModel):
    items: list[RulePreviewEntry]
    total: int
    page: int
    per_page: int
    total_pages: int
    metadata: RulePreviewMetadata | None = None


class ReclaimHistoryAttributes(BaseModel):
    resolution: str | None = None
    hdr: bool | None = None
    dolby_vision: bool | None = None


class ReclaimHistoryEntry(BaseModel):
    id: int
    approved_by: str
    media_type: str
    tmdb_id: int | None
    name: str | None
    size: int | None
    attributes: ReclaimHistoryAttributes | None = None
    action: str = "deleted"
    destination_path: str | None = None
    created_at: str


class PaginatedReclaimHistoryResponse(BaseModel):
    items: list[ReclaimHistoryEntry]
    total: int
    page: int
    per_page: int
    total_pages: int
