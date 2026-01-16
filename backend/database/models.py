from __future__ import annotations

from datetime import datetime

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
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column

from backend.enums import MediaType, Service, TaskStatus


class Base(MappedAsDataclass, DeclarativeBase):
    """Base class for all models."""

    pass


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
    # service specific settings
    extra_settings: Mapped[dict | None] = mapped_column(JSON, default=None, init=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        init=False,
    )


class Movie(Base):
    """Movie availability and metadata."""

    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    # basic info
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int] = mapped_column(SmallInteger)
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)

    # file info
    size: Mapped[int | None] = mapped_column(Integer, default=None)
    library_name: Mapped[str | None] = mapped_column(String(255), default=None)

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
    never_watched: Mapped[bool] = mapped_column(Boolean, default=True)

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


class Series(Base):
    """Series availability and metadata."""

    __tablename__ = "series"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )

    # basic info
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int] = mapped_column(SmallInteger)
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)

    # file info
    size: Mapped[int | None] = mapped_column(Integer, default=None)
    library_name: Mapped[str | None] = mapped_column(String(255), default=None)

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
    never_watched: Mapped[bool] = mapped_column(Boolean, default=True)

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


class CleanupRule(Base):
    """User-defined cleanup rules for movies and series."""

    __tablename__ = "cleanup_rules"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, init=False, autoincrement=True
    )
    name: Mapped[str] = mapped_column(String(100))  # rule name
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # library filtering (applies to Plex/Jellyfin library names)
    # None = applies to all libraries, otherwise specific library name
    library_name: Mapped[str | None] = mapped_column(String(255), default=None)

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

    # size criteria (bytes)
    min_size: Mapped[int | None] = mapped_column(Integer, default=None)
    max_size: Mapped[int | None] = mapped_column(Integer, default=None)

    # actions
    auto_tag: Mapped[bool] = mapped_column(Boolean, default=True)

    # metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class CleanupCandidate(Base):
    """Items identified as candidates for deletion based on cleanup rules."""

    __tablename__ = "cleanup_candidates"

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

    # foreign keys (one will be set based on media_type)
    movie_id: Mapped[int | None] = mapped_column(
        ForeignKey("movies.id"), unique=True, default=None
    )
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("series.id"), unique=True, default=None
    )

    # library context
    library_name: Mapped[str | None] = mapped_column(String(255), default=None)

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


class TaskRun(Base):
    """Track scheduled task execution history and status."""

    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)

    # scan_cleanup_candidates, sync_watch_history, etc.
    task_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.PENDING
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
