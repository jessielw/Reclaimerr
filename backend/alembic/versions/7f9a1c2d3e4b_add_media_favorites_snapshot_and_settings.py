"""add media favorites snapshot and settings

Revision ID: 7f9a1c2d3e4b
Revises: 8e7d6c5b4a3f
Create Date: 2026-05-20 22:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f9a1c2d3e4b"
down_revision: str | Sequence[str] | None = "8e7d6c5b4a3f"
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
    if not _table_exists("media_favorites"):
        op.create_table(
            "media_favorites",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(
                "media_type",
                sa.Enum("MOVIE", "SERIES", name="mediatype"),
                nullable=False,
            ),
            sa.Column("tmdb_id", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=255), nullable=False),
            sa.Column("username_normalized", sa.String(length=255), nullable=False),
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
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
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
                "username_normalized",
                "source_service",
                "source_service_config_id",
                name="uq_media_favorites_identity",
            ),
        )

    media_favorites_indexes = {
        "ix_media_favorites_media_type": ["media_type"],
        "ix_media_favorites_tmdb_id": ["tmdb_id"],
        "ix_media_favorites_username_normalized": ["username_normalized"],
        "ix_media_favorites_source_service": ["source_service"],
        "ix_media_favorites_source_service_config_id": ["source_service_config_id"],
    }
    for name, columns in media_favorites_indexes.items():
        if not _index_exists(name):
            op.create_index(name, "media_favorites", columns)

    general_cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "favorites_ignore_enabled" not in general_cols:
            batch_op.add_column(
                sa.Column(
                    "favorites_ignore_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
        if "favorites_protect_all_users" not in general_cols:
            batch_op.add_column(
                sa.Column(
                    "favorites_protect_all_users",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
        if "favorites_usernames" not in general_cols:
            batch_op.add_column(
                sa.Column(
                    "favorites_usernames",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[]'"),
                )
            )


def downgrade() -> None:
    if _table_exists("media_favorites"):
        op.drop_table("media_favorites")

    general_cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "favorites_usernames" in general_cols:
            batch_op.drop_column("favorites_usernames")
        if "favorites_protect_all_users" in general_cols:
            batch_op.drop_column("favorites_protect_all_users")
        if "favorites_ignore_enabled" in general_cols:
            batch_op.drop_column("favorites_ignore_enabled")
