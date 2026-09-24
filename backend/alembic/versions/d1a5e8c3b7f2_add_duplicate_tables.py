"""Add episode_versions, duplicate_ignores and the duplicate keeper setting.

episode_versions holds every physical file the main media server reports for an
episode (movies already have movie_versions). It starts empty and the next
series sync fills it; nothing else reads it besides duplicate detection.

Revision ID: d1a5e8c3b7f2
Revises: c4e7b0a9d316
Create Date: 2026-09-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1a5e8c3b7f2"
down_revision: str | Sequence[str] | None = "c4e7b0a9d316"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SERVICE_ENUM = sa.Enum(
    "SONARR",
    "RADARR",
    "JELLYFIN",
    "EMBY",
    "PLEX",
    "SEERR",
    "TAUTULLI",
    "TRACEARR",
    "MDBLIST",
    "OMDB",
    name="service",
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    tables = _tables()

    if "episode_versions" not in tables:
        op.create_table(
            "episode_versions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("episode_id", sa.Integer(), nullable=False),
            sa.Column("service", _SERVICE_ENUM, nullable=False),
            sa.Column("service_item_id", sa.String(length=100), nullable=False),
            sa.Column("service_media_id", sa.String(length=100), nullable=False),
            sa.Column("library_id", sa.String(length=100), nullable=False),
            sa.Column("library_name", sa.String(length=255), nullable=False),
            sa.Column("path", sa.String(length=1024), nullable=True),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("added_at", sa.DateTime(), nullable=True),
            sa.Column("video_resolution", sa.String(length=20), nullable=True),
            sa.Column("video_width", sa.Integer(), nullable=True),
            sa.Column("video_height", sa.Integer(), nullable=True),
            sa.Column("video_codec_family", sa.String(length=24), nullable=True),
            sa.Column("video_hdr", sa.Boolean(), nullable=True),
            sa.Column("video_dolby_vision", sa.Boolean(), nullable=True),
            sa.Column("video_bitrate", sa.Integer(), nullable=True),
            sa.Column("audio_codec_family", sa.String(length=24), nullable=True),
            sa.Column("audio_channels", sa.SmallInteger(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["episode_id"], ["episodes.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "episode_id", "service", "service_media_id", name="uq_episode_version"
            ),
        )
        with op.batch_alter_table("episode_versions", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_episode_versions_episode_id"),
                ["episode_id"],
                unique=False,
            )

    if "duplicate_ignores" not in tables:
        op.create_table(
            "duplicate_ignores",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(
                "media_type",
                sa.Enum("MOVIE", "SERIES", name="mediatype"),
                nullable=False,
            ),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "media_type", "item_id", name="uq_duplicate_ignore_item"
            ),
        )
        with op.batch_alter_table("duplicate_ignores", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_duplicate_ignores_item_id"), ["item_id"], unique=False
            )

    if "duplicate_keeper_priority" not in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("duplicate_keeper_priority", sa.JSON(), nullable=True)
            )


def downgrade() -> None:
    if "duplicate_keeper_priority" in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("duplicate_keeper_priority")

    tables = _tables()
    if "duplicate_ignores" in tables:
        op.drop_table("duplicate_ignores")
    if "episode_versions" in tables:
        op.drop_table("episode_versions")
