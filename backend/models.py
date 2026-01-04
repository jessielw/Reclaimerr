from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    MappedAsDataclass,
    mapped_column,
    relationship,
)

from backend.enums import MediaType, SeriesStatus, Service, TaskStatus


class Base(MappedAsDataclass, DeclarativeBase):
    """Base class for all models."""

    pass


class ServiceConfig(Base):
    """
    Configuration for external services (Plex, Jellyfin, Radarr, Sonarr, Seerr).
    """

    __tablename__ = "service_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    service_type: Mapped[Service] = mapped_column(Enum(Service))
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.utcnow(), onupdate=func.utcnow(), init=False
    )


class CleanupRule(Base):
    """User-defined cleanup rules for movies and series."""

    __tablename__ = "cleanup_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType))

    # rule criteria
    unwatched_days: Mapped[int | None] = mapped_column(Integer, default=None)
    last_watched_days: Mapped[int | None] = mapped_column(Integer, default=None)
    max_imdb_rating: Mapped[float | None] = mapped_column(Float, default=None)
    min_file_size_gb: Mapped[float | None] = mapped_column(Float, default=None)

    # actions
    auto_tag: Mapped[bool] = mapped_column(
        Boolean, default=True
    )  # TODO: should we auto tag? research this!

    # series-specific
    delete_partial_seasons: Mapped[bool] = mapped_column(Boolean, default=False)
    ended_series_grace_days: Mapped[int | None] = mapped_column(Integer, default=None)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )


class Movie(Base):
    """Cached movie metadata."""

    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)

    radarr_id: Mapped[int] = mapped_column(Integer, unique=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    imdb_id: Mapped[str | None] = mapped_column(String(20), default=None, init=False)
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int] = mapped_column(SmallInteger)
    imdb_rating: Mapped[float | None] = mapped_column(Float, default=None, init=False)
    tmdb_rating: Mapped[float | None] = mapped_column(Float, default=None, init=False)
    file_path: Mapped[str | None] = mapped_column(String(500), default=None, init=False)
    file_size_bytes: Mapped[int | None] = mapped_column(
        Integer, default=None, init=False
    )
    poster_url: Mapped[str | None] = mapped_column(
        String(500), default=None, init=False
    )
    added_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )

    # relationships
    watch_history: Mapped[list[WatchHistory]] = relationship(
        back_populates="movie", default_factory=list, init=False
    )
    cleanup_candidate: Mapped[CleanupCandidate | None] = relationship(
        back_populates="movie", default=None, init=False
    )


class Series(Base):
    """Cached series metadata."""

    __tablename__ = "series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)

    sonarr_id: Mapped[int] = mapped_column(Integer, unique=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    imdb_id: Mapped[str | None] = mapped_column(String(20), default=None, init=False)
    tvdb_id: Mapped[str | None] = mapped_column(String(20), default=None, init=False)
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int] = mapped_column(SmallInteger)
    imdb_rating: Mapped[float | None] = mapped_column(Float, default=None, init=False)
    tmdb_rating: Mapped[float | None] = mapped_column(Float, default=None, init=False)
    tvdb_rating: Mapped[float | None] = mapped_column(Float, default=None, init=False)
    poster_url: Mapped[str | None] = mapped_column(
        String(500), default=None, init=False
    )

    # series info
    status: Mapped[SeriesStatus | None] = mapped_column(
        Enum(SeriesStatus), default=None, init=False
    )
    season_count: Mapped[int | None] = mapped_column(Integer, default=None, init=False)

    # dates
    added_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )

    # relationships
    watch_history: Mapped[list[WatchHistory]] = relationship(
        back_populates="series", default_factory=list, init=False
    )
    cleanup_candidate: Mapped[CleanupCandidate | None] = relationship(
        back_populates="series", default=None, init=False
    )


class WatchHistory(Base):
    """Watch history from Plex/Jellyfin - tracks when media was last watched."""

    __tablename__ = "watch_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)

    # foreign keys (one of these will be set)
    movie_id: Mapped[int | None] = mapped_column(
        ForeignKey("movies.id"), unique=True, default=None, init=False
    )
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("series.id"), unique=True, default=None, init=False
    )

    # watch data - only what matters for cleanup decisions
    last_viewed_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, init=False
    )
    view_count: Mapped[int] = mapped_column(Integer, default=0)

    # relationships
    movie: Mapped[Movie | None] = relationship(
        back_populates="watch_history", default=None, init=False
    )
    series: Mapped[Series | None] = relationship(
        back_populates="watch_history", default=None, init=False
    )

    synced_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )


class CleanupCandidate(Base):
    """Items identified as candidates for deletion based on cleanup rules."""

    __tablename__ = "cleanup_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)

    # foreign keys (one of these will be set)
    movie_id: Mapped[int | None] = mapped_column(
        ForeignKey("movies.id"), unique=True, default=None, init=False
    )
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("series.id"), unique=True, default=None, init=False
    )

    # candidate info -> why it was marked (e.g., "Unwatched for 365 days")
    reason: Mapped[str] = mapped_column(Text)
    matched_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("cleanup_rules.id"), default=None, init=False
    )

    # status
    tagged_in_arr: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)  # user reviewed
    approved_for_deletion: Mapped[bool] = mapped_column(Boolean, default=False)

    # space savings
    estimated_space_gb: Mapped[float | None] = mapped_column(
        Float, default=None, init=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), init=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), init=False
    )

    # relationships
    movie: Mapped[Movie | None] = relationship(
        back_populates="cleanup_candidate", default=None, init=False
    )
    series: Mapped[Series | None] = relationship(
        back_populates="cleanup_candidate", default=None, init=False
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
