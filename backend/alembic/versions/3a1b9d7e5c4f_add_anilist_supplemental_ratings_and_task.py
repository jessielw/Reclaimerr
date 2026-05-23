"""add anilist supplemental ratings columns/state and scheduled task

Revision ID: 3a1b9d7e5c4f
Revises: 2f4e6d8c1b0a
Create Date: 2026-05-23 11:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a1b9d7e5c4f"
down_revision: str | Sequence[str] | None = "2f4e6d8c1b0a"
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
    name="task",
)


def upgrade() -> None:
    op.create_table(
        "anilist_ratings_ingest_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_url", sa.String(length=500), nullable=False),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("last_modified", sa.String(length=255), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("content_length", sa.BigInteger(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_successful_refresh_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("movies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("anilist_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("anilist_score", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("anilist_popularity", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("anilist_favourites", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("anilist_refreshed_at", sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f("ix_movies_anilist_id"), ["anilist_id"], unique=False)

    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.add_column(sa.Column("anilist_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("anilist_score", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("anilist_popularity", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("anilist_favourites", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("anilist_refreshed_at", sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f("ix_series_anilist_id"), ["anilist_id"], unique=False)

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
    op.execute(sa.text("DELETE FROM task_runs WHERE task = 'ANILIST_RATINGS_REFRESH'"))
    op.execute(
        sa.text("DELETE FROM task_schedules WHERE task = 'ANILIST_RATINGS_REFRESH'")
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

    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_series_anilist_id"))
        batch_op.drop_column("anilist_refreshed_at")
        batch_op.drop_column("anilist_favourites")
        batch_op.drop_column("anilist_popularity")
        batch_op.drop_column("anilist_score")
        batch_op.drop_column("anilist_id")

    with op.batch_alter_table("movies", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_movies_anilist_id"))
        batch_op.drop_column("anilist_refreshed_at")
        batch_op.drop_column("anilist_favourites")
        batch_op.drop_column("anilist_popularity")
        batch_op.drop_column("anilist_score")
        batch_op.drop_column("anilist_id")

    op.drop_table("anilist_ratings_ingest_state")
