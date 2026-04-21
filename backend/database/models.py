from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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

    # last updated
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class ServiceConfig(Base):
    """
    Configuration for external services (Plex, Jellyfin, Radarr, Sonarr, Seerr).
    """

    __tablename__ = "service_configs"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    service_type: Mapped[Service] = mapped_column(Enum(Service), unique=True)
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # designates this as the sole source-of-truth for physical file versions;
    # only one Plex/Jellyfin server may have is_main=True at a time
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
    auto_tag_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_tag_suffix: Mapped[str] = mapped_column(String(15), default="")
    worker_poll_min_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    worker_poll_max_seconds: Mapped[float | None] = mapped_column(Float, default=None)

    # timestamps
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
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
    radarr_id: Mapped[int | None] = mapped_column(
        Integer, unique=True, index=True, default=None
    )
    imdb_id: Mapped[str | None] = mapped_column(
        String(20), unique=True, index=True, default=None
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

    # watch tracking (from Plex/Jellyfin)
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
    # plex ratingKey or jellyfin item ID (used for item-level ops like delete)
    service_item_id: Mapped[str] = mapped_column(String(100))
    # plex Media.id or jellyfin MediaSource.Id (unique per physical file)
    service_media_id: Mapped[str] = mapped_column(String(100))

    # library
    library_id: Mapped[str] = mapped_column(String(100))
    library_name: Mapped[str] = mapped_column(String(255))

    # file info
    path: Mapped[str | None] = mapped_column(String(1024), default=None)
    size: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    container: Mapped[str | None] = mapped_column(String(20), default=None)

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
    # plex ratingKey or jellyfin item ID (used for item-level ops like delete)
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
    sonarr_id: Mapped[int | None] = mapped_column(
        Integer, unique=True, index=True, default=None
    )
    imdb_id: Mapped[str | None] = mapped_column(
        String(20), unique=True, index=True, default=None
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

    # series-specific info
    season_count: Mapped[int | None] = mapped_column(Integer, default=None, init=False)

    # watch tracking (from Plex/Jellyfin)
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

    # service-specific IDs for direct ops
    jellyfin_season_id: Mapped[str | None] = mapped_column(String(100), default=None)
    emby_season_id: Mapped[str | None] = mapped_column(String(100), default=None)
    plex_season_rating_key: Mapped[str | None] = mapped_column(
        String(100), default=None
    )

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


class ReclaimRule(Base):
    """User-defined reclaim rules for movies and series."""

    __tablename__ = "reclaim_rules"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    name: Mapped[str] = mapped_column(String(100))  # rule name
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # library filtering (stores ServiceMediaLibrary.library_id values)
    # None or empty list = applies to all libraries
    # Otherwise, item must have library_id in one of the listed IDs
    library_ids: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    # TMDB popularity criteria
    min_popularity: Mapped[float | None] = mapped_column(Float, default=None)
    max_popularity: Mapped[float | None] = mapped_column(Float, default=None)

    # TMDB vote average criteria
    min_vote_average: Mapped[float | None] = mapped_column(Float, default=None)
    max_vote_average: Mapped[float | None] = mapped_column(Float, default=None)

    # TMDB vote count criteria
    min_vote_count: Mapped[int | None] = mapped_column(Integer, default=None)
    max_vote_count: Mapped[int | None] = mapped_column(Integer, default=None)

    # watch history criteria
    min_view_count: Mapped[int | None] = mapped_column(Integer, default=None)
    max_view_count: Mapped[int | None] = mapped_column(Integer, default=None)
    include_never_watched: Mapped[bool] = mapped_column(Boolean, default=False)

    # age criteria (days since added)
    min_days_since_added: Mapped[int | None] = mapped_column(Integer, default=None)
    max_days_since_added: Mapped[int | None] = mapped_column(Integer, default=None)

    # watch recency criteria (days since last watched)
    min_days_since_last_watched: Mapped[int | None] = mapped_column(
        Integer, default=None
    )
    max_days_since_last_watched: Mapped[int | None] = mapped_column(
        Integer, default=None
    )

    # size criteria (bytes)
    min_size: Mapped[int | None] = mapped_column(Integer, default=None)
    max_size: Mapped[int | None] = mapped_column(Integer, default=None)

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

    # foreign keys (movie_id or series_id will be set based on media_type)
    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id"), default=None)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), default=None)
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey("seasons.id"), default=None, index=True
    )

    # workflow status
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_for_deletion: Mapped[bool] = mapped_column(Boolean, default=False)
    tagged_in_arr: Mapped[bool] = mapped_column(Boolean, default=False)

    # space savings
    estimated_space_gb: Mapped[float | None] = mapped_column(Float, default=None)

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
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), default=None)
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey("seasons.id"), default=None, index=True
    )

    # optional details
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    protected_by: Mapped[User] = relationship(init=False, lazy="noload", repr=False)

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
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), default=None)
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey("seasons.id"), default=None, index=True
    )

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
