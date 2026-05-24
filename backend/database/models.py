from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.enums import (
    BackgroundJobStatus,
    BackgroundJobType,
    MediaType,
    ProtectionRequestStatus,
    ScheduleType,
    Service,
    Task,
    TaskStatus,
    UserRole,
)
from backend.types.media import AudioCodecFamily, VideoCodecFamily


class User(Base):
    """User account for Reclaimerr."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    username: Mapped[str] = mapped_column(String(32), unique=True, init=True)
    password_hash: Mapped[str] = mapped_column(String(255), init=True)
    email: Mapped[str | None] = mapped_column(String(120), unique=True, default=None)
    display_name: Mapped[str | None] = mapped_column(String(32), default=None)

    # permissions
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER)
    permissions: Mapped[list[str]] = mapped_column(JSON, default_factory=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    require_password_change: Mapped[bool] = mapped_column(Boolean, default=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0)

    # metadata
    avatar_path: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )

    # relationships
    notification_settings: Mapped[list[NotificationSetting]] = relationship(
        back_populates="user", default_factory=list, lazy="noload", repr=False
    )

    def bump_token_version(self) -> None:
        """
        Increment token version to invalidate existing sessions.

        Must be called within a session commit block to take effect.
        """
        self.token_version += 1


class NotificationSetting(Base):
    """Notification settings."""

    __tablename__ = "notification_settings"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    # relationships
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    user: Mapped[User] = relationship(
        back_populates="notification_settings", init=False, lazy="noload", repr=False
    )

    enabled: Mapped[bool] = mapped_column(Boolean)
    url: Mapped[str] = mapped_column(String(500))
    name: Mapped[str | None] = mapped_column(String(100), default=None)

    # notification types
    new_cleanup_candidates: Mapped[bool] = mapped_column(Boolean, default=False)
    request_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    request_declined: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_message: Mapped[bool] = mapped_column(Boolean, default=False)
    # admin notification types
    task_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    # per notification content preferences (formatting/detail controls)
    preferences: Mapped[dict | None] = mapped_column(JSON, default=None)

    # last updated
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class ServiceConfig(Base):
    """
    Configuration for external services (Media servers, Radarr, Sonarr, Seerr).
    """

    __tablename__ = "service_configs"
    __table_args__ = (
        UniqueConstraint("service_type", "name", name="uq_service_config_type_name"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    service_type: Mapped[Service] = mapped_column(Enum(Service), index=True)
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(100), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # designates this as the sole source-of-truth for physical file versions;
    # only one media server may have is_main=True at a time
    is_main: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        init=False,
    )


class ServiceMediaLibrary(Base):
    """Media libraries available from the main media server."""

    __tablename__ = "service_media_libraries"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    library_id: Mapped[str] = mapped_column(String(50))
    library_name: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class GeneralSettings(Base):
    """General application settings."""

    __tablename__ = "general_settings"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    # cleanup and tagging settings
    worker_poll_min_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    worker_poll_max_seconds: Mapped[float | None] = mapped_column(Float, default=None)

    # path mapping (media-server paths -> local paths)
    path_mappings: Mapped[list] = mapped_column(JSON, default_factory=list)

    # post action webhooks (generic hooks fired after successful delete/move)
    post_action_webhooks: Mapped[list] = mapped_column(JSON, default_factory=list)

    # move settings
    move_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    move_destination_movies: Mapped[str | None] = mapped_column(
        String(1024), default=None
    )
    move_destination_series: Mapped[str | None] = mapped_column(
        String(1024), default=None
    )

    # deletion routing
    media_server_fallback_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_arr_delete_behavior: Mapped[str] = mapped_column(
        String(32), default="unmonitor"
    )
    add_arr_import_exclusions_on_delete: Mapped[bool] = mapped_column(
        Boolean, default=True
    )

    # favorites
    favorites_ignore_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    favorites_protect_all_users: Mapped[bool] = mapped_column(Boolean, default=False)
    favorites_usernames: Mapped[list[str]] = mapped_column(JSON, default_factory=list)
    requester_watch_user_mappings: Mapped[list] = mapped_column(
        JSON, default_factory=list
    )

    # timestamps
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class AppUpdateState(Base):
    """Persist latest app update check result."""

    __tablename__ = "app_update_state"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    current_version: Mapped[str | None] = mapped_column(String(64), default=None)
    latest_version: Mapped[str | None] = mapped_column(String(64), default=None)
    latest_release_url: Mapped[str | None] = mapped_column(String(500), default=None)
    latest_release_published_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    update_available: Mapped[bool] = mapped_column(Boolean, default=False)
    last_check_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class IMDbRatingsIngestState(Base):
    """Track fetch/cache metadata for IMDb title ratings dataset ingest."""

    __tablename__ = "imdb_ratings_ingest_state"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    dataset_url: Mapped[str] = mapped_column(String(500), default="")
    etag: Mapped[str | None] = mapped_column(String(255), default=None)
    last_modified: Mapped[str | None] = mapped_column(String(255), default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    content_length: Mapped[int | None] = mapped_column(BigInteger, default=None)
    row_count: Mapped[int | None] = mapped_column(Integer, default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_successful_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class IMDbTitleRating(Base):
    """Cached IMDb rating/vote rows keyed by IMDb title ID."""

    __tablename__ = "imdb_title_ratings"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    imdb_id: Mapped[str] = mapped_column(String(20), unique=True)
    rating: Mapped[float | None] = mapped_column(Float, default=None)
    vote_count: Mapped[int | None] = mapped_column(Integer, default=None)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class AniListRatingsIngestState(Base):
    """Track fetch/cache metadata for AniBridge + AniList ratings ingest."""

    __tablename__ = "anilist_ratings_ingest_state"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    dataset_url: Mapped[str] = mapped_column(String(500), default="")
    etag: Mapped[str | None] = mapped_column(String(255), default=None)
    last_modified: Mapped[str | None] = mapped_column(String(255), default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    content_length: Mapped[int | None] = mapped_column(BigInteger, default=None)
    row_count: Mapped[int | None] = mapped_column(Integer, default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_successful_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class AdminNotice(Base):
    """Persisted admin facing in app notices with global read state."""

    __tablename__ = "admin_notices"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_admin_notices_dedupe_key"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    kind: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    action_label: Mapped[str | None] = mapped_column(String(100), default=None)
    action_href: Mapped[str | None] = mapped_column(String(500), default=None)
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    read_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    dedupe_key: Mapped[str | None] = mapped_column(
        String(120), default=None, index=True
    )
    last_occurred_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class Movie(Base):
    """Movie availability and metadata."""

    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    # basic info
    title: Mapped[str] = mapped_column(String(512))
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    year: Mapped[int | None] = mapped_column(SmallInteger, default=None)

    # aggregate file size (sum of all versions - updated during sync)
    size: Mapped[int | None] = mapped_column(Integer, default=None)

    # external IDs
    imdb_id: Mapped[str | None] = mapped_column(
        String(20), unique=True, index=True, default=None
    )
    imdb_rating: Mapped[float | None] = mapped_column(Float, default=None)
    imdb_vote_count: Mapped[int | None] = mapped_column(Integer, default=None)
    imdb_ratings_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )
    anilist_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    anilist_score: Mapped[int | None] = mapped_column(Integer, default=None)
    anilist_popularity: Mapped[int | None] = mapped_column(Integer, default=None)
    anilist_favourites: Mapped[int | None] = mapped_column(Integer, default=None)
    anilist_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )

    # metadata info
    tmdb_title: Mapped[str | None] = mapped_column(String(512), default=None)
    original_title: Mapped[str | None] = mapped_column(String(512), default=None)
    tmdb_release_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    original_language: Mapped[str | None] = mapped_column(String(10), default=None)
    homepage: Mapped[str | None] = mapped_column(String(500), default=None)
    origin_country: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    poster_url: Mapped[str | None] = mapped_column(String(500), default=None)
    backdrop_url: Mapped[str | None] = mapped_column(
        String(500), default=None, init=False
    )
    overview: Mapped[str | None] = mapped_column(Text, default=None)
    genres: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    popularity: Mapped[float | None] = mapped_column(Float, default=None)
    vote_average: Mapped[float | None] = mapped_column(Float, default=None)
    vote_count: Mapped[int | None] = mapped_column(Integer, default=None)
    revenue: Mapped[int | None] = mapped_column(Integer, default=None)
    runtime: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str | None] = mapped_column(String(50), default=None)
    tagline: Mapped[str | None] = mapped_column(String(255), default=None)
    arr_tags: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    # monitoring status synced from Radarr (OR across multiple instances)
    is_monitored: Mapped[bool | None] = mapped_column(Boolean, default=None)

    # watch tracking (from media server)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    view_count: Mapped[int] = mapped_column(Integer, default=0)

    # lifecycle tracking
    added_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    # we're only soft deleting data to maintain TMDB metadata on re-add
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )

    # cache freshness
    last_metadata_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )

    # relationships
    versions: Mapped[list[MovieVersion]] = relationship(
        back_populates="movie",
        default_factory=list,
        lazy="noload",
        repr=False,
        cascade="all, delete-orphan",
    )
    protection_requests: Mapped[list[ProtectionRequest]] = relationship(
        back_populates="movie", default_factory=list, lazy="noload", repr=False
    )
    delete_requests: Mapped[list[DeleteRequest]] = relationship(
        back_populates="movie", default_factory=list, lazy="noload", repr=False
    )


class MovieVersion(Base):
    """Individual physical file version of a movie."""

    __tablename__ = "movie_versions"
    __table_args__ = (
        UniqueConstraint(
            "movie_id", "service", "service_media_id", name="uq_movie_version"
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id"), index=True)

    # service identification
    service: Mapped[Service] = mapped_column(Enum(Service))
    # plex ratingKey or jellyfin/emby item ID (used for item-level ops like delete)
    service_item_id: Mapped[str] = mapped_column(String(100))
    # plex Media.id or jellyfin/emby MediaSource.Id (unique per physical file)
    service_media_id: Mapped[str] = mapped_column(String(100))

    # library
    library_id: Mapped[str] = mapped_column(String(100))
    library_name: Mapped[str] = mapped_column(String(255))

    # file info
    path: Mapped[str | None] = mapped_column(String(1024), default=None)
    size: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    file_name: Mapped[str | None] = mapped_column(String(255), default=None)
    container: Mapped[str | None] = mapped_column(String(20), default=None)
    duration: Mapped[float | None] = mapped_column(Float, default=None)

    # video stream metadata
    video_track_count: Mapped[int | None] = mapped_column(Integer, default=None)
    video_codec: Mapped[str | None] = mapped_column(String(80), default=None)
    video_codec_family: Mapped[VideoCodecFamily | None] = mapped_column(
        String(24), default=None
    )
    video_hdr: Mapped[bool | None] = mapped_column(Boolean, default=None)
    video_dolby_vision: Mapped[bool | None] = mapped_column(Boolean, default=None)
    video_dolby_vision_profile: Mapped[str | None] = mapped_column(
        String(50), default=None
    )
    video_bitrate: Mapped[int | None] = mapped_column(Integer, default=None)
    video_bit_depth: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    video_width: Mapped[int | None] = mapped_column(Integer, default=None)
    video_height: Mapped[int | None] = mapped_column(Integer, default=None)
    video_resolution: Mapped[str | None] = mapped_column(String(20), default=None)
    video_color_primaries: Mapped[str | None] = mapped_column(String(50), default=None)
    video_color_space: Mapped[str | None] = mapped_column(String(50), default=None)
    video_color_transfer: Mapped[str | None] = mapped_column(String(50), default=None)
    video_fps: Mapped[float | None] = mapped_column(Float, default=None)

    # audio stream metadata
    audio_count: Mapped[int | None] = mapped_column(Integer, default=None)
    audio_languages: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    audio_codec: Mapped[str | None] = mapped_column(String(80), default=None)
    audio_codec_family: Mapped[AudioCodecFamily | None] = mapped_column(
        String(24), default=None
    )
    audio_title: Mapped[str | None] = mapped_column(String(255), default=None)
    audio_language: Mapped[str | None] = mapped_column(String(20), default=None)
    audio_channels: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    audio_channel_layout: Mapped[str | None] = mapped_column(String(100), default=None)
    audio_bitrate: Mapped[int | None] = mapped_column(Integer, default=None)
    audio_sample_rate: Mapped[int | None] = mapped_column(Integer, default=None)

    # subtitle stream metadata
    subtitle_count: Mapped[int | None] = mapped_column(Integer, default=None)
    subtitle_has_forced: Mapped[bool | None] = mapped_column(Boolean, default=None)
    subtitle_languages: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    # chapter metadata
    has_chapters: Mapped[bool | None] = mapped_column(Boolean, default=None)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )

    # relationships
    movie: Mapped[Movie] = relationship(
        back_populates="versions", init=False, lazy="noload", repr=False
    )


class SeriesServiceRef(Base):
    """Service-specific reference for a series (one row per service that has it)."""

    __tablename__ = "series_service_refs"
    __table_args__ = (
        UniqueConstraint("series_id", "service", name="uq_series_service_ref"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"), index=True)

    # service identification
    service: Mapped[Service] = mapped_column(Enum(Service))
    # plex ratingKey or jellyfin/emby item ID (used for item-level ops like delete)
    service_id: Mapped[str] = mapped_column(String(100))

    # library
    library_id: Mapped[str] = mapped_column(String(100))
    library_name: Mapped[str] = mapped_column(String(255))

    # file info
    path: Mapped[str | None] = mapped_column(String(1024), default=None)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )

    # relationships
    series: Mapped[Series] = relationship(
        back_populates="service_refs", init=False, lazy="noload", repr=False
    )


class SupplementalMediaMatch(Base):
    """High confidence identity map for supplemental same media services."""

    __tablename__ = "supplemental_media_matches"
    __table_args__ = (
        UniqueConstraint(
            "source_service",
            "source_item_id",
            "media_type",
            name="uq_supplemental_media_match_source_item",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    source_service: Mapped[Service] = mapped_column(Enum(Service), index=True)
    source_item_id: Mapped[str] = mapped_column(String(100))
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType), index=True)

    movie_id: Mapped[int | None] = mapped_column(
        ForeignKey("movies.id"), index=True, default=None
    )
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("series.id"), index=True, default=None
    )
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey("seasons.id"), index=True, default=None
    )
    source_media_id: Mapped[str | None] = mapped_column(String(100), default=None)
    path_tail: Mapped[str | None] = mapped_column(String(1024), default=None)
    confidence: Mapped[int] = mapped_column(SmallInteger, default=100)
    signals: Mapped[dict | None] = mapped_column(JSON, default=None)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class MediaFavorite(Base):
    """Snapshot of user favorites mapped to TMDB IDs across media servers.

    Emby and Jellyfin only for now as Plex doesn't store this data locally.
    """

    __tablename__ = "media_favorites"
    __table_args__ = (
        UniqueConstraint(
            "media_type",
            "tmdb_id",
            "username_normalized",
            "source_service",
            "source_service_config_id",
            name="uq_media_favorites_identity",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    username: Mapped[str] = mapped_column(String(255))
    username_normalized: Mapped[str] = mapped_column(String(255), index=True)
    source_service: Mapped[Service] = mapped_column(Enum(Service), index=True)
    source_service_config_id: Mapped[int] = mapped_column(
        ForeignKey("service_configs.id", ondelete="CASCADE"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class MediaWatchUser(Base):
    """User watch snapshot rows mapped to TMDB IDs across media servers."""

    __tablename__ = "media_watch_users"
    __table_args__ = (
        UniqueConstraint(
            "media_type",
            "tmdb_id",
            "watch_user_key_normalized",
            "source_service",
            "source_service_config_id",
            name="uq_media_watch_users_identity",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    watch_user_key: Mapped[str] = mapped_column(String(255))
    watch_user_key_normalized: Mapped[str] = mapped_column(String(255), index=True)
    source_service: Mapped[Service] = mapped_column(Enum(Service), index=True)
    source_service_config_id: Mapped[int] = mapped_column(
        ForeignKey("service_configs.id", ondelete="CASCADE"),
        index=True,
    )
    last_watched_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    play_count: Mapped[int | None] = mapped_column(Integer, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class Series(Base):
    """Series availability and metadata."""

    __tablename__ = "series"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    # basic info
    title: Mapped[str] = mapped_column(String(512))
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    year: Mapped[int | None] = mapped_column(SmallInteger, default=None)

    # file info
    size: Mapped[int | None] = mapped_column(Integer, default=None)

    # external IDs
    imdb_id: Mapped[str | None] = mapped_column(
        String(20), unique=True, index=True, default=None
    )
    imdb_rating: Mapped[float | None] = mapped_column(Float, default=None)
    imdb_vote_count: Mapped[int | None] = mapped_column(Integer, default=None)
    imdb_ratings_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )
    anilist_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    anilist_score: Mapped[int | None] = mapped_column(Integer, default=None)
    anilist_popularity: Mapped[int | None] = mapped_column(Integer, default=None)
    anilist_favourites: Mapped[int | None] = mapped_column(Integer, default=None)
    anilist_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )
    tvdb_id: Mapped[str | None] = mapped_column(
        String(20), unique=True, index=True, default=None
    )

    # metadata info
    tmdb_title: Mapped[str | None] = mapped_column(String(512), default=None)
    original_title: Mapped[str | None] = mapped_column(String(512), default=None)
    tmdb_first_air_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    tmdb_last_air_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    original_language: Mapped[str | None] = mapped_column(String(10), default=None)
    homepage: Mapped[str | None] = mapped_column(String(500), default=None)
    origin_country: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    poster_url: Mapped[str | None] = mapped_column(String(500), default=None)
    backdrop_url: Mapped[str | None] = mapped_column(
        String(500), default=None, init=False
    )
    overview: Mapped[str | None] = mapped_column(Text, default=None)
    genres: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    popularity: Mapped[float | None] = mapped_column(Float, default=None)
    vote_average: Mapped[float | None] = mapped_column(Float, default=None)
    vote_count: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str | None] = mapped_column(String(50), default=None)
    tagline: Mapped[str | None] = mapped_column(String(255), default=None)
    arr_tags: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    # monitoring status synced from Sonarr (OR across multiple instances)
    is_monitored: Mapped[bool | None] = mapped_column(Boolean, default=None)

    # series-specific info
    season_count: Mapped[int | None] = mapped_column(Integer, default=None, init=False)

    # watch tracking (from plex/jellyfin/emby)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    # aggregate media signals
    has_hdr: Mapped[bool | None] = mapped_column(Boolean, default=None)
    has_dolby_vision: Mapped[bool | None] = mapped_column(Boolean, default=None)
    max_video_width: Mapped[int | None] = mapped_column(Integer, default=None)
    max_video_height: Mapped[int | None] = mapped_column(Integer, default=None)
    video_codec_families: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    audio_codec_families: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    max_audio_channels: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    subtitle_languages: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    # lifecycle tracking
    added_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    # we're only soft deleting data to maintain TMDB metadata on re-add
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )

    # cache freshness
    last_metadata_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )

    # relationships
    service_refs: Mapped[list[SeriesServiceRef]] = relationship(
        back_populates="series",
        default_factory=list,
        lazy="noload",
        repr=False,
        cascade="all, delete-orphan",
    )
    seasons: Mapped[list[Season]] = relationship(
        back_populates="series",
        default_factory=list,
        lazy="noload",
        repr=False,
        cascade="all, delete-orphan",
    )
    protection_requests: Mapped[list[ProtectionRequest]] = relationship(
        back_populates="series", default_factory=list, lazy="noload", repr=False
    )
    delete_requests: Mapped[list[DeleteRequest]] = relationship(
        back_populates="series", default_factory=list, lazy="noload", repr=False
    )


class Season(Base):
    """Per-season data for a series (watch stats, size, service IDs)."""

    __tablename__ = "seasons"
    __table_args__ = (
        UniqueConstraint("series_id", "season_number", name="uq_season_series_number"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"), index=True)
    season_number: Mapped[int] = mapped_column(SmallInteger)

    # aggregate stats
    size: Mapped[int | None] = mapped_column(Integer, default=None)
    episode_count: Mapped[int | None] = mapped_column(Integer, default=None)
    view_count: Mapped[int | None] = mapped_column(Integer, default=None)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    air_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    path: Mapped[str | None] = mapped_column(String(1024), default=None)
    episode_paths: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    # aggregate media signals
    has_hdr: Mapped[bool | None] = mapped_column(Boolean, default=None)
    has_dolby_vision: Mapped[bool | None] = mapped_column(Boolean, default=None)
    max_video_width: Mapped[int | None] = mapped_column(Integer, default=None)
    max_video_height: Mapped[int | None] = mapped_column(Integer, default=None)
    video_codec_families: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    audio_codec_families: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    audio_languages: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    max_audio_channels: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    subtitle_languages: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    # service-specific IDs for direct ops
    jellyfin_season_id: Mapped[str | None] = mapped_column(String(100), default=None)
    emby_season_id: Mapped[str | None] = mapped_column(String(100), default=None)
    plex_season_rating_key: Mapped[str | None] = mapped_column(
        String(100), default=None
    )

    # monitoring status synced from Sonarr (OR across multiple instances)
    is_monitored: Mapped[bool | None] = mapped_column(Boolean, default=None)

    added_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )

    # relationships
    series: Mapped[Series] = relationship(
        back_populates="seasons", init=False, lazy="noload", repr=False
    )
    episodes: Mapped[list[Episode]] = relationship(
        back_populates="season",
        default_factory=list,
        lazy="noload",
        repr=False,
        cascade="all, delete-orphan",
    )


class Episode(Base):
    """Episode data for a season (watch stats, air date, file path)."""

    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint(
            "season_id", "episode_number", name="uq_episode_season_number"
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), index=True
    )
    episode_number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(500), default=None)

    # file info
    air_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    size: Mapped[int | None] = mapped_column(Integer, default=None)
    path: Mapped[str | None] = mapped_column(String(1024), default=None)

    # watch tracking
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    # service specific IDs
    plex_rating_key: Mapped[str | None] = mapped_column(String(100), default=None)
    jellyfin_episode_id: Mapped[str | None] = mapped_column(String(100), default=None)
    emby_episode_id: Mapped[str | None] = mapped_column(String(100), default=None)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )

    # relationships
    season: Mapped[Season] = relationship(
        back_populates="episodes", init=False, lazy="noload", repr=False
    )


class MovieArrRef(Base):
    """Per *arr instance reference for a movie."""

    __tablename__ = "movie_arr_refs"
    __table_args__ = (
        UniqueConstraint(
            "service_config_id", "arr_movie_id", name="uq_movie_arr_ref_service_arr_id"
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id"), index=True)
    service_config_id: Mapped[int] = mapped_column(
        ForeignKey("service_configs.id"), index=True
    )
    arr_movie_id: Mapped[int] = mapped_column(Integer)
    arr_movie_path: Mapped[str | None] = mapped_column(String(2048), default=None)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class SeriesArrRef(Base):
    """Per *arr instance reference for a series."""

    __tablename__ = "series_arr_refs"
    __table_args__ = (
        UniqueConstraint(
            "service_config_id",
            "arr_series_id",
            name="uq_series_arr_ref_service_arr_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"), index=True)
    service_config_id: Mapped[int] = mapped_column(
        ForeignKey("service_configs.id"), index=True
    )
    arr_series_id: Mapped[int] = mapped_column(Integer)
    arr_series_path: Mapped[str | None] = mapped_column(String(2048), default=None)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class ReclaimRule(Base):
    """User-defined advanced reclaim rules for movies and series."""

    __tablename__ = "reclaim_rules"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    name: Mapped[str] = mapped_column(String(100))  # rule name
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # advanced rule engine fields
    target_scope: Mapped[str | None] = mapped_column(String(32), default=None)
    definition: Mapped[dict | None] = mapped_column(JSON, default=None)
    action: Mapped[dict | None] = mapped_column(JSON, default=None)

    # metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class ReclaimCandidate(Base):
    """Items identified as candidates for deletion based on reclaim rules."""

    __tablename__ = "reclaim_candidates"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    # media identification (required for TMDB ID tv/movie id overlap)
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))

    # multiple rule matching support (required fields first for dataclass)
    matched_rule_ids: Mapped[list[int]] = mapped_column(JSON)
    # snapshot of values: {"popularity": 2.5, "vote_average": 4.2, "view_count": 0}
    matched_criteria: Mapped[dict] = mapped_column(JSON)
    # easily readable combined reasons from all matched rules
    reason: Mapped[str] = mapped_column(Text)
    # structured reason payload produced by advanced rule engine
    reason_data: Mapped[list[dict] | None] = mapped_column(JSON, default=None)

    # foreign keys (movie_id or series_id will be set based on media_type)
    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id"), default=None)
    movie_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("movie_versions.id", ondelete="SET NULL"), default=None, index=True
    )
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), default=None)
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey("seasons.id"), default=None, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="SET NULL"), default=None, index=True
    )

    # workflow status
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_for_deletion: Mapped[bool] = mapped_column(Boolean, default=False)
    tagged_in_arr: Mapped[bool] = mapped_column(Boolean, default=False)

    # space savings
    estimated_space_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    delete_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_delete_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )
    last_delete_error: Mapped[str | None] = mapped_column(Text, default=None)

    # timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class ProtectedMedia(Base):
    """Media items protected from deletion/cleanup."""

    __tablename__ = "protected_media"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    # media identification
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))

    # protected details (required fields first for dataclass)
    protected_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )

    # foreign keys (movie_id or series_id will be set based on media_type)
    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id"), default=None)
    movie_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("movie_versions.id", ondelete="SET NULL"), default=None, index=True
    )
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), default=None)
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey("seasons.id"), default=None, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), default=None, index=True
    )

    # optional details
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    protected_by: Mapped[User] = relationship(init=False, lazy="noload", repr=False)
    season: Mapped[Season | None] = relationship(init=False, lazy="noload", repr=False)
    episode: Mapped[Episode | None] = relationship(
        init=False,
        lazy="noload",
        repr=False,
    )

    # expiration options
    permanent: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    # timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class ProtectionRequest(Base):
    """User requests for media to be excluded from cleanup."""

    __tablename__ = "protection_requests"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    # media identification
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))

    # request details (required fields first for dataclass)
    requested_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    requested_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )

    # foreign keys (movie_id or series_id will be set based on media_type)
    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id"), default=None)
    movie_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("movie_versions.id", ondelete="SET NULL"), default=None, index=True
    )
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), default=None)
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey("seasons.id"), default=None, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), default=None, index=True
    )
    target_scope: Mapped[str | None] = mapped_column(String(32), default=None)
    season_number_snapshot: Mapped[int | None] = mapped_column(Integer, default=None)
    episode_number_snapshot: Mapped[int | None] = mapped_column(Integer, default=None)
    episode_name_snapshot: Mapped[str | None] = mapped_column(String(500), default=None)

    # relationships
    movie: Mapped[Movie | None] = relationship(
        back_populates="protection_requests",
        init=False,
        lazy="noload",
        repr=False,
    )
    series: Mapped[Series | None] = relationship(
        back_populates="protection_requests",
        init=False,
        lazy="noload",
        repr=False,
    )
    season: Mapped[Season | None] = relationship(
        init=False,
        lazy="noload",
        repr=False,
    )
    episode: Mapped[Episode | None] = relationship(
        init=False,
        lazy="noload",
        repr=False,
    )

    # candidate reference (if it was a candidate when requested)
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("reclaim_candidates.id", ondelete="SET NULL"), default=None
    )

    requested_by: Mapped[User] = relationship(
        foreign_keys=[requested_by_user_id], init=False, lazy="noload", repr=False
    )

    # status: pending, approved, denied
    status: Mapped[ProtectionRequestStatus] = mapped_column(
        Enum(ProtectionRequestStatus), default=ProtectionRequestStatus.PENDING
    )

    # admin review
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    reviewed_by: Mapped[User | None] = relationship(
        foreign_keys=[reviewed_by_user_id], init=False, lazy="noload", repr=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    admin_notes: Mapped[str | None] = mapped_column(Text, default=None)

    # timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class DeleteRequest(Base):
    """User requests for media to be deleted."""

    __tablename__ = "delete_requests"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))

    requested_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id"), default=None)
    movie_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("movie_versions.id", ondelete="SET NULL"), default=None, index=True
    )
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), default=None)
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey("seasons.id"), default=None, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), default=None, index=True
    )
    target_scope: Mapped[str | None] = mapped_column(String(32), default=None)
    season_number_snapshot: Mapped[int | None] = mapped_column(Integer, default=None)
    episode_number_snapshot: Mapped[int | None] = mapped_column(Integer, default=None)
    episode_name_snapshot: Mapped[str | None] = mapped_column(String(500), default=None)

    movie: Mapped[Movie | None] = relationship(
        back_populates="delete_requests",
        init=False,
        lazy="noload",
        repr=False,
    )
    series: Mapped[Series | None] = relationship(
        back_populates="delete_requests",
        init=False,
        lazy="noload",
        repr=False,
    )
    season: Mapped[Season | None] = relationship(
        init=False,
        lazy="noload",
        repr=False,
    )
    episode: Mapped[Episode | None] = relationship(
        init=False,
        lazy="noload",
        repr=False,
    )

    requested_by: Mapped[User] = relationship(
        foreign_keys=[requested_by_user_id], init=False, lazy="noload", repr=False
    )

    status: Mapped[ProtectionRequestStatus] = mapped_column(
        Enum(ProtectionRequestStatus), default=ProtectionRequestStatus.PENDING
    )

    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    reviewed_by: Mapped[User | None] = relationship(
        foreign_keys=[reviewed_by_user_id], init=False, lazy="noload", repr=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    admin_notes: Mapped[str | None] = mapped_column(Text, default=None)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    execution_error: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class ReclaimHistory(Base):
    """Historical record of reclaim actions taken on media items."""

    __tablename__ = "reclaim_history"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    approved_by: Mapped[str] = mapped_column(String(32))
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))
    tmdb_id: Mapped[int | None] = mapped_column(Integer, index=True)
    path: Mapped[str | None] = mapped_column(String(1024), default=None)
    name: Mapped[str | None] = mapped_column(String(255), default=None)
    size: Mapped[int | None] = mapped_column(Integer, default=None)
    attributes: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    action: Mapped[str] = mapped_column(String(20), default="deleted")
    destination_path: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )


class TaskRun(Base):
    """Track scheduled task execution history and status."""

    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)

    task: Mapped[Task] = mapped_column(Enum(Task))
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.SCHEDULED
    )

    # relationship to task schedule
    task_schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_schedules.id"), default=None
    )
    task_schedule: Mapped[TaskSchedule | None] = relationship(
        back_populates="task_runs", init=False, lazy="noload", repr=False
    )

    # results
    items_processed: Mapped[int | None] = mapped_column(
        Integer, default=None, init=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, default=None, init=False)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )


class TaskSchedule(Base):
    """Store configurable task schedules for the scheduler."""

    __tablename__ = "task_schedules"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    task: Mapped[Task] = mapped_column(Enum(Task))

    # schedule type: interval or cron
    schedule_type: Mapped[ScheduleType] = mapped_column(Enum(ScheduleType))

    # schedule value - either interval in seconds (for INTERVAL) or cron expression (for CRON)
    # Examples: "28800" for 8 hours, "0 2 * * *" for daily at 2 AM
    schedule_value: Mapped[str] = mapped_column(String(100))

    # default schedule values (for reset functionality)
    default_schedule_type: Mapped[ScheduleType] = mapped_column(Enum(ScheduleType))
    default_schedule_value: Mapped[str] = mapped_column(String(100))

    # description
    description: Mapped[str | None] = mapped_column(Text, default=None)

    # whether the job is enabled
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # relationships
    task_runs: Mapped[list[TaskRun]] = relationship(
        back_populates="task_schedule", default_factory=list, lazy="noload", repr=False
    )

    # timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class BackgroundJob(Base):
    """Durable background work item executed by the standalone worker."""

    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    job_type: Mapped[BackgroundJobType] = mapped_column(Enum(BackgroundJobType))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[BackgroundJobStatus] = mapped_column(
        Enum(BackgroundJobStatus), default=BackgroundJobStatus.PENDING
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default_factory=dict)
    dedupe_key: Mapped[str | None] = mapped_column(
        String(120), default=None, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    claimed_by: Mapped[str | None] = mapped_column(String(120), default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, default=None, init=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )
