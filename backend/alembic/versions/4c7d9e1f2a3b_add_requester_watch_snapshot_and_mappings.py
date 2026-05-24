"""Add requester watch snapshot table and general-settings user mappings.

Revision ID: 4c7d9e1f2a3b
Revises: 3a1b9d7e5c4f
Create Date: 2026-05-23 17:20:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "4c7d9e1f2a3b"
down_revision: str | Sequence[str] | None = "3a1b9d7e5c4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name LIMIT 1"
        ),
        {"name": table},
    ).first()
    return row is not None


def _index_exists(index: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :name LIMIT 1"
        ),
        {"name": index},
    ).first()
    return row is not None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    if not _table_exists("media_watch_users"):
        op.create_table(
            "media_watch_users",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(
                "media_type",
                sa.Enum("MOVIE", "SERIES", name="mediatype"),
                nullable=False,
            ),
            sa.Column("tmdb_id", sa.Integer(), nullable=False),
            sa.Column("watch_user_key", sa.String(length=255), nullable=False),
            sa.Column(
                "watch_user_key_normalized", sa.String(length=255), nullable=False
            ),
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
            sa.Column("last_watched_at", sa.DateTime(), nullable=False),
            sa.Column("play_count", sa.Integer(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["source_service_config_id"],
                ["service_configs.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "media_type",
                "tmdb_id",
                "watch_user_key_normalized",
                "source_service",
                "source_service_config_id",
                name="uq_media_watch_users_identity",
            ),
        )

    watch_indexes = {
        "ix_media_watch_users_media_type": ["media_type"],
        "ix_media_watch_users_tmdb_id": ["tmdb_id"],
        "ix_media_watch_users_watch_user_key_normalized": [
            "watch_user_key_normalized"
        ],
        "ix_media_watch_users_source_service": ["source_service"],
        "ix_media_watch_users_source_service_config_id": ["source_service_config_id"],
        "ix_media_watch_users_last_watched_at": ["last_watched_at"],
    }
    for name, columns in watch_indexes.items():
        if not _index_exists(name):
            op.create_index(name, "media_watch_users", columns)

    general_cols = _cols("general_settings")
    if "requester_watch_user_mappings" not in general_cols:
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "requester_watch_user_mappings",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[]'"),
                )
            )


def downgrade() -> None:
    general_cols = _cols("general_settings")
    if "requester_watch_user_mappings" in general_cols:
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("requester_watch_user_mappings")

    if _table_exists("media_watch_users"):
        op.drop_table("media_watch_users")
