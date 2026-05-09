"""add supplemental media matches

Revision ID: e9f1a2b3c4d5
Revises: d8e9f0a1b2c3
Create Date: 2026-05-09 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f1a2b3c4d5"
down_revision: str | None = "d8e9f0a1b2c3"
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


def upgrade() -> None:
    if not _table_exists("supplemental_media_matches"):
        op.create_table(
            "supplemental_media_matches",
            sa.Column("id", sa.Integer(), nullable=False),
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
            sa.Column("source_item_id", sa.String(length=100), nullable=False),
            sa.Column(
                "media_type",
                sa.Enum("MOVIE", "SERIES", name="mediatype"),
                nullable=False,
            ),
            sa.Column("movie_id", sa.Integer(), nullable=True),
            sa.Column("series_id", sa.Integer(), nullable=True),
            sa.Column("season_id", sa.Integer(), nullable=True),
            sa.Column("source_media_id", sa.String(length=100), nullable=True),
            sa.Column("path_tail", sa.String(length=1024), nullable=True),
            sa.Column("confidence", sa.SmallInteger(), nullable=False),
            sa.Column("signals", sa.JSON(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["movie_id"], ["movies.id"]),
            sa.ForeignKeyConstraint(["season_id"], ["seasons.id"]),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_service",
                "source_item_id",
                "media_type",
                name="uq_supplemental_media_match_source_item",
            ),
        )

    indexes = {
        "ix_supplemental_media_matches_source_service": ["source_service"],
        "ix_supplemental_media_matches_media_type": ["media_type"],
        "ix_supplemental_media_matches_movie_id": ["movie_id"],
        "ix_supplemental_media_matches_series_id": ["series_id"],
        "ix_supplemental_media_matches_season_id": ["season_id"],
    }
    for name, columns in indexes.items():
        if not _index_exists(name):
            op.create_index(name, "supplemental_media_matches", columns)


def downgrade() -> None:
    if _table_exists("supplemental_media_matches"):
        op.drop_table("supplemental_media_matches")
