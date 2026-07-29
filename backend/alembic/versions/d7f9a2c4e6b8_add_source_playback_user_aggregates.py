"""Add source-aware playback user aggregates.

Revision ID: d7f9a2c4e6b8
Revises: c6e8a1d4f9b2
Create Date: 2026-07-28 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7f9a2c4e6b8"
down_revision: str | None = "c6e8a1d4f9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playback_history_user_aggregates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_service_config_id", sa.Integer(), nullable=False),
        sa.Column(
            "observed_service",
            sa.Enum(
                "SONARR",
                "RADARR",
                "JELLYFIN",
                "EMBY",
                "PLEX",
                "SEERR",
                "TAUTULLI",
                "MDBLIST",
                "OMDB",
                name="service",
            ),
            nullable=False,
        ),
        sa.Column("target_scope", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column(
            "media_type",
            sa.Enum("MOVIE", "SERIES", name="mediatype"),
            nullable=False,
        ),
        sa.Column("unique_user_count", sa.Integer(), nullable=False),
        sa.Column("usernames", sa.JSON(), nullable=False),
        sa.Column("usernames_complete", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_service_config_id"],
            ["service_configs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_service_config_id",
            "observed_service",
            "target_scope",
            "target_id",
            name="uq_playback_history_user_aggregate_source_target",
        ),
    )
    for name, columns in {
        "ix_playback_history_user_aggregates_source_service_config_id": [
            "source_service_config_id"
        ],
        "ix_playback_history_user_aggregates_observed_service": [
            "observed_service"
        ],
        "ix_playback_history_user_aggregates_target_scope": ["target_scope"],
        "ix_playback_history_user_aggregates_target_id": ["target_id"],
        "ix_playback_history_user_aggregates_media_type": ["media_type"],
    }.items():
        op.create_index(
            name,
            "playback_history_user_aggregates",
            columns,
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("playback_history_user_aggregates")
