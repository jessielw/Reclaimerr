"""Add persisted media-server-native playback snapshots.

Revision ID: b9c2d4e6f8a0
Revises: a7c9e2d4f6b8
Create Date: 2026-07-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "b9c2d4e6f8a0"
down_revision = "a7c9e2d4f6b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_playback_users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_service",
            sa.Enum(
                "SONARR", "RADARR", "JELLYFIN", "EMBY", "PLEX", "SEERR", "TAUTULLI",
                name="service",
            ),
            nullable=False,
        ),
        sa.Column("source_service_config_id", sa.Integer(), nullable=False),
        sa.Column("source_item_id", sa.String(length=255), nullable=False),
        sa.Column("provider_media_type", sa.String(length=16), nullable=False),
        sa.Column("source_user_id", sa.String(length=255), nullable=False),
        sa.Column("source_username", sa.String(length=255), nullable=True),
        sa.Column("source_username_normalized", sa.String(length=255), nullable=True),
        sa.Column("play_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["source_service_config_id"], ["service_configs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_service_config_id", "source_item_id", "source_user_id", name="uq_native_playback_user_source"),
    )
    for name, columns in {
        "ix_native_playback_users_source_service": ["source_service"],
        "ix_native_playback_users_source_service_config_id": ["source_service_config_id"],
        "ix_native_playback_users_source_item_id": ["source_item_id"],
        "ix_native_playback_users_provider_media_type": ["provider_media_type"],
        "ix_native_playback_users_source_user_id": ["source_user_id"],
        "ix_native_playback_users_source_username_normalized": ["source_username_normalized"],
        "ix_native_playback_users_completed": ["completed"],
        "ix_native_playback_users_last_activity_at": ["last_activity_at"],
    }.items():
        op.create_index(name, "native_playback_users", columns)

    op.create_table(
        "native_playback_aggregates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_service",
            sa.Enum(
                "SONARR", "RADARR", "JELLYFIN", "EMBY", "PLEX", "SEERR", "TAUTULLI",
                name="service",
            ),
            nullable=False,
        ),
        sa.Column("source_service_config_id", sa.Integer(), nullable=False),
        sa.Column("target_scope", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.Enum("MOVIE", "SERIES", name="mediatype"), nullable=False),
        sa.Column("has_activity", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("play_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unique_user_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usernames", sa.JSON(), nullable=False),
        sa.Column("usernames_complete", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["source_service_config_id"], ["service_configs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_service_config_id", "target_scope", "target_id", name="uq_native_playback_aggregate_target"),
    )
    for name, columns in {
        "ix_native_playback_aggregates_source_service": ["source_service"],
        "ix_native_playback_aggregates_source_service_config_id": ["source_service_config_id"],
        "ix_native_playback_aggregates_target_scope": ["target_scope"],
        "ix_native_playback_aggregates_target_id": ["target_id"],
        "ix_native_playback_aggregates_media_type": ["media_type"],
        "ix_native_playback_aggregates_last_activity_at": ["last_activity_at"],
    }.items():
        op.create_index(name, "native_playback_aggregates", columns)


def downgrade() -> None:
    op.drop_table("native_playback_aggregates")
    op.drop_table("native_playback_users")
