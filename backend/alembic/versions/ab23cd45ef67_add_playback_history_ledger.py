"""add playback history ledger

Revision ID: ab23cd45ef67
Revises: d6e7f8a9b0c1
Create Date: 2026-06-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab23cd45ef67"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playback_history_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "source_service",
            sa.Enum(
                "SONARR",
                "RADARR",
                "JELLYFIN",
                "EMBY",
                "PLEX",
                "SEERR",
                "TAUTULLI",
                name="service",
            ),
            nullable=False,
        ),
        sa.Column("source_service_config_id", sa.Integer(), nullable=False),
        sa.Column("source_event_key", sa.String(length=255), nullable=False),
        sa.Column("source_item_id", sa.String(length=255), nullable=False),
        sa.Column("provider_media_type", sa.String(length=16), nullable=False),
        sa.Column("played_at", sa.DateTime(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("source_user_id", sa.String(length=255), nullable=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("season_number", sa.SmallInteger(), nullable=True),
        sa.Column("episode_number", sa.Integer(), nullable=True),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("season_id", sa.Integer(), nullable=True),
        sa.Column("episode_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"], ["episodes.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_service_config_id"],
            ["service_configs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_service_config_id",
            "source_event_key",
            name="uq_playback_history_event_source",
        ),
    )
    for name, columns in {
        "ix_playback_history_events_source_service": ["source_service"],
        "ix_playback_history_events_source_service_config_id": [
            "source_service_config_id"
        ],
        "ix_playback_history_events_source_item_id": ["source_item_id"],
        "ix_playback_history_events_source_user_id": ["source_user_id"],
        "ix_playback_history_events_played_at": ["played_at"],
        "ix_playback_history_events_tmdb_id": ["tmdb_id"],
        "ix_playback_history_events_movie_id": ["movie_id"],
        "ix_playback_history_events_series_id": ["series_id"],
        "ix_playback_history_events_season_id": ["season_id"],
        "ix_playback_history_events_episode_id": ["episode_id"],
    }.items():
        op.create_index(name, "playback_history_events", columns, unique=False)

    op.create_table(
        "playback_history_aggregates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("target_scope", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column(
            "media_type",
            sa.Enum("MOVIE", "SERIES", name="mediatype"),
            nullable=False,
        ),
        sa.Column("play_count", sa.Integer(), nullable=False),
        sa.Column("total_duration_seconds", sa.BigInteger(), nullable=False),
        sa.Column("longest_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("unique_user_count", sa.Integer(), nullable=False),
        sa.Column("first_activity_at", sa.DateTime(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_scope",
            "target_id",
            name="uq_playback_history_aggregate_target",
        ),
    )
    for name, columns in {
        "ix_playback_history_aggregates_target_scope": ["target_scope"],
        "ix_playback_history_aggregates_target_id": ["target_id"],
        "ix_playback_history_aggregates_media_type": ["media_type"],
        "ix_playback_history_aggregates_last_activity_at": ["last_activity_at"],
    }.items():
        op.create_index(name, "playback_history_aggregates", columns, unique=False)


def downgrade() -> None:
    op.drop_table("playback_history_aggregates")
    op.drop_table("playback_history_events")
