"""multi arr refs drop legacy ids

Revision ID: c3a4f93e21aa
Revises: 9d2a6c7f4b10
Create Date: 2026-04-29 23:40:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3a4f93e21aa"
down_revision: Union[str, None] = "9d2a6c7f4b10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


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
    with op.batch_alter_table("movies", schema=None) as batch_op:
        if "radarr_id" in _cols("movies"):
            if _index_exists("ix_movies_radarr_id"):
                batch_op.drop_index("ix_movies_radarr_id")
            batch_op.drop_column("radarr_id")

    with op.batch_alter_table("series", schema=None) as batch_op:
        if "sonarr_id" in _cols("series"):
            if _index_exists("ix_series_sonarr_id"):
                batch_op.drop_index("ix_series_sonarr_id")
            batch_op.drop_column("sonarr_id")

    if not _table_exists("movie_arr_refs"):
        op.create_table(
            "movie_arr_refs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("movie_id", sa.Integer(), nullable=False),
            sa.Column("service_config_id", sa.Integer(), nullable=False),
            sa.Column("arr_movie_id", sa.Integer(), nullable=False),
            sa.Column("tmdb_id", sa.Integer(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["movie_id"], ["movies.id"]),
            sa.ForeignKeyConstraint(["service_config_id"], ["service_configs.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "service_config_id",
                "arr_movie_id",
                name="uq_movie_arr_ref_service_arr_id",
            ),
        )
    if not _index_exists("ix_movie_arr_refs_movie_id"):
        op.create_index("ix_movie_arr_refs_movie_id", "movie_arr_refs", ["movie_id"])
    if not _index_exists("ix_movie_arr_refs_service_config_id"):
        op.create_index(
            "ix_movie_arr_refs_service_config_id",
            "movie_arr_refs",
            ["service_config_id"],
        )
    if not _index_exists("ix_movie_arr_refs_tmdb_id"):
        op.create_index("ix_movie_arr_refs_tmdb_id", "movie_arr_refs", ["tmdb_id"])

    if not _table_exists("series_arr_refs"):
        op.create_table(
            "series_arr_refs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("series_id", sa.Integer(), nullable=False),
            sa.Column("service_config_id", sa.Integer(), nullable=False),
            sa.Column("arr_series_id", sa.Integer(), nullable=False),
            sa.Column("tmdb_id", sa.Integer(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
            sa.ForeignKeyConstraint(["service_config_id"], ["service_configs.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "service_config_id",
                "arr_series_id",
                name="uq_series_arr_ref_service_arr_id",
            ),
        )
    if not _index_exists("ix_series_arr_refs_series_id"):
        op.create_index("ix_series_arr_refs_series_id", "series_arr_refs", ["series_id"])
    if not _index_exists("ix_series_arr_refs_service_config_id"):
        op.create_index(
            "ix_series_arr_refs_service_config_id",
            "series_arr_refs",
            ["service_config_id"],
        )
    if not _index_exists("ix_series_arr_refs_tmdb_id"):
        op.create_index("ix_series_arr_refs_tmdb_id", "series_arr_refs", ["tmdb_id"])


def downgrade() -> None:
    if _table_exists("series_arr_refs"):
        if _index_exists("ix_series_arr_refs_tmdb_id"):
            op.drop_index("ix_series_arr_refs_tmdb_id", table_name="series_arr_refs")
        if _index_exists("ix_series_arr_refs_service_config_id"):
            op.drop_index(
                "ix_series_arr_refs_service_config_id", table_name="series_arr_refs"
            )
        if _index_exists("ix_series_arr_refs_series_id"):
            op.drop_index("ix_series_arr_refs_series_id", table_name="series_arr_refs")
        op.drop_table("series_arr_refs")

    if _table_exists("movie_arr_refs"):
        if _index_exists("ix_movie_arr_refs_tmdb_id"):
            op.drop_index("ix_movie_arr_refs_tmdb_id", table_name="movie_arr_refs")
        if _index_exists("ix_movie_arr_refs_service_config_id"):
            op.drop_index(
                "ix_movie_arr_refs_service_config_id", table_name="movie_arr_refs"
            )
        if _index_exists("ix_movie_arr_refs_movie_id"):
            op.drop_index("ix_movie_arr_refs_movie_id", table_name="movie_arr_refs")
        op.drop_table("movie_arr_refs")

    with op.batch_alter_table("series", schema=None) as batch_op:
        if "sonarr_id" not in _cols("series"):
            batch_op.add_column(sa.Column("sonarr_id", sa.Integer(), nullable=True))
        if not _index_exists("ix_series_sonarr_id"):
            batch_op.create_index("ix_series_sonarr_id", ["sonarr_id"], unique=True)

    with op.batch_alter_table("movies", schema=None) as batch_op:
        if "radarr_id" not in _cols("movies"):
            batch_op.add_column(sa.Column("radarr_id", sa.Integer(), nullable=True))
        if not _index_exists("ix_movies_radarr_id"):
            batch_op.create_index("ix_movies_radarr_id", ["radarr_id"], unique=True)
