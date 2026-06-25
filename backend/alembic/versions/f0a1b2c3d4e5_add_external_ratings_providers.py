"""add external ratings provider fields and task

Revision ID: f0a1b2c3d4e5
Revises: cd8a74b3e901
Create Date: 2026-06-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "cd8a74b3e901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TASK = sa.Enum(
    "SYNC_MEDIA",
    "RESYNC_MEDIA",
    "SYNC_MEDIA_LIBRARIES",
    "SYNC_LINKED_DATA",
    "SCAN_CLEANUP_CANDIDATES",
    "TAG_CLEANUP_CANDIDATES",
    "DELETE_CLEANUP_CANDIDATES",
    "WEEKLY_HOUSE_KEEPING",
    "CHECK_APP_UPDATES",
    "IMDB_RATINGS_REFRESH",
    "ANILIST_RATINGS_REFRESH",
    "REFRESH_PLAYBACK_HISTORY",
    name="task",
)
_NEW_TASK = sa.Enum(
    "SYNC_MEDIA",
    "RESYNC_MEDIA",
    "SYNC_MEDIA_LIBRARIES",
    "SYNC_LINKED_DATA",
    "SCAN_CLEANUP_CANDIDATES",
    "TAG_CLEANUP_CANDIDATES",
    "DELETE_CLEANUP_CANDIDATES",
    "WEEKLY_HOUSE_KEEPING",
    "CHECK_APP_UPDATES",
    "IMDB_RATINGS_REFRESH",
    "ANILIST_RATINGS_REFRESH",
    "REFRESH_EXTERNAL_RATINGS",
    "REFRESH_PLAYBACK_HISTORY",
    name="task",
)
_OLD_SERVICE = sa.Enum(
    "SONARR",
    "RADARR",
    "JELLYFIN",
    "EMBY",
    "PLEX",
    "SEERR",
    "TAUTULLI",
    name="service",
)
_NEW_SERVICE = sa.Enum(
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
)
_SERVICE_COLUMNS = (
    ("service_configs", "service_type", False),
    ("media_user_identities", "source_service", False),
    ("movie_versions", "service", False),
    ("series_service_refs", "service", False),
    ("supplemental_media_matches", "source_service", False),
    ("media_favorites", "source_service", False),
    ("media_watch_users", "source_service", False),
    ("playback_history_events", "source_service", False),
)


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    result = bind.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    ).first()
    return result is not None


def _add_external_rating_columns(table: str) -> None:
    existing = _cols(table)
    with op.batch_alter_table(table, schema=None) as batch_op:
        if "rottentomatoes_tomato_meter" not in existing:
            batch_op.add_column(
                sa.Column("rottentomatoes_tomato_meter", sa.Integer(), nullable=True)
            )
        if "rottentomatoes_tomato_vote_count" not in existing:
            batch_op.add_column(
                sa.Column(
                    "rottentomatoes_tomato_vote_count", sa.Integer(), nullable=True
                )
            )
        if "rottentomatoes_popcorn_meter" not in existing:
            batch_op.add_column(
                sa.Column("rottentomatoes_popcorn_meter", sa.Integer(), nullable=True)
            )
        if "rottentomatoes_popcorn_vote_count" not in existing:
            batch_op.add_column(
                sa.Column(
                    "rottentomatoes_popcorn_vote_count", sa.Integer(), nullable=True
                )
            )
        if "metacritic_metascore" not in existing:
            batch_op.add_column(
                sa.Column("metacritic_metascore", sa.Integer(), nullable=True)
            )
        if "metacritic_vote_count" not in existing:
            batch_op.add_column(
                sa.Column("metacritic_vote_count", sa.Integer(), nullable=True)
            )
        if "metacritic_user_score" not in existing:
            batch_op.add_column(
                sa.Column("metacritic_user_score", sa.Integer(), nullable=True)
            )
        if "metacritic_user_vote_count" not in existing:
            batch_op.add_column(
                sa.Column("metacritic_user_vote_count", sa.Integer(), nullable=True)
            )
        if "trakt_rating" not in existing:
            batch_op.add_column(sa.Column("trakt_rating", sa.Integer(), nullable=True))
        if "trakt_vote_count" not in existing:
            batch_op.add_column(
                sa.Column("trakt_vote_count", sa.Integer(), nullable=True)
            )
        if "letterboxd_score" not in existing:
            batch_op.add_column(
                sa.Column("letterboxd_score", sa.Integer(), nullable=True)
            )
        if "letterboxd_vote_count" not in existing:
            batch_op.add_column(
                sa.Column("letterboxd_vote_count", sa.Integer(), nullable=True)
            )
        if "external_ratings_source" not in existing:
            batch_op.add_column(
                sa.Column("external_ratings_source", sa.String(length=64), nullable=True)
            )
        if "external_ratings_refreshed_at" not in existing:
            batch_op.add_column(
                sa.Column("external_ratings_refreshed_at", sa.DateTime(), nullable=True)
            )


def _drop_external_rating_columns(table: str) -> None:
    existing = _cols(table)
    with op.batch_alter_table(table, schema=None) as batch_op:
        for column in (
            "external_ratings_refreshed_at",
            "external_ratings_source",
            "letterboxd_vote_count",
            "letterboxd_score",
            "trakt_vote_count",
            "trakt_rating",
            "metacritic_user_vote_count",
            "metacritic_user_score",
            "metacritic_vote_count",
            "metacritic_metascore",
            "rottentomatoes_popcorn_vote_count",
            "rottentomatoes_popcorn_meter",
            "rottentomatoes_tomato_vote_count",
            "rottentomatoes_tomato_meter",
        ):
            if column in existing:
                batch_op.drop_column(column)


def _alter_service_enum(existing_type: sa.Enum, new_type: sa.Enum) -> None:
    for table, column, nullable in _SERVICE_COLUMNS:
        if _table_exists(table) and column in _cols(table):
            with op.batch_alter_table(table, schema=None) as batch_op:
                batch_op.alter_column(
                    column,
                    existing_type=existing_type,
                    type_=new_type,
                    existing_nullable=nullable,
                )


def upgrade() -> None:
    _alter_service_enum(_OLD_SERVICE, _NEW_SERVICE)

    if not _table_exists("external_ratings_ingest_state"):
        op.create_table(
            "external_ratings_ingest_state",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("provider_summary", sa.JSON(), nullable=True),
            sa.Column("movie_count", sa.Integer(), nullable=True),
            sa.Column("series_count", sa.Integer(), nullable=True),
            sa.Column("request_count", sa.Integer(), nullable=True),
            sa.Column("updated_count", sa.Integer(), nullable=True),
            sa.Column("last_checked_at", sa.DateTime(), nullable=True),
            sa.Column("last_successful_refresh_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
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
            sa.PrimaryKeyConstraint("id"),
        )

    _add_external_rating_columns("movies")
    _add_external_rating_columns("series")

    with op.batch_alter_table("task_schedules", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_OLD_TASK,
            type_=_NEW_TASK,
            existing_nullable=False,
        )

    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_OLD_TASK,
            type_=_NEW_TASK,
            existing_nullable=False,
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM task_runs WHERE task = 'REFRESH_EXTERNAL_RATINGS'")
    )
    op.execute(
        sa.text("DELETE FROM task_schedules WHERE task = 'REFRESH_EXTERNAL_RATINGS'")
    )
    op.execute(
        sa.text("DELETE FROM service_configs WHERE service_type IN ('MDBLIST', 'OMDB')")
    )

    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_NEW_TASK,
            type_=_OLD_TASK,
            existing_nullable=False,
        )

    with op.batch_alter_table("task_schedules", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_NEW_TASK,
            type_=_OLD_TASK,
            existing_nullable=False,
        )

    _drop_external_rating_columns("series")
    _drop_external_rating_columns("movies")

    if _table_exists("external_ratings_ingest_state"):
        op.drop_table("external_ratings_ingest_state")

    _alter_service_enum(_NEW_SERVICE, _OLD_SERVICE)
