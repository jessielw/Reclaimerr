"""add imdb ratings cache tables and scheduled task

Revision ID: 2f4e6d8c1b0a
Revises: 7f9a1c2d3e4b
Create Date: 2026-05-21 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2f4e6d8c1b0a"
down_revision: str | Sequence[str] | None = "7f9a1c2d3e4b"
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
    name="task",
)


def upgrade() -> None:
    op.create_table(
        "imdb_ratings_ingest_state",
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

    op.create_table(
        "imdb_title_ratings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("imdb_id", sa.String(length=20), nullable=False),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("vote_count", sa.Integer(), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(), nullable=True),
        sa.Column(
            "refreshed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("imdb_id"),
    )

    with op.batch_alter_table("movies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("imdb_rating", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("imdb_vote_count", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("imdb_ratings_refreshed_at", sa.DateTime(), nullable=True)
        )

    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.add_column(sa.Column("imdb_rating", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("imdb_vote_count", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("imdb_ratings_refreshed_at", sa.DateTime(), nullable=True)
        )

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
        sa.text("DELETE FROM task_runs WHERE task = 'IMDB_RATINGS_REFRESH'")
    )
    op.execute(
        sa.text("DELETE FROM task_schedules WHERE task = 'IMDB_RATINGS_REFRESH'")
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
        batch_op.drop_column("imdb_ratings_refreshed_at")
        batch_op.drop_column("imdb_vote_count")
        batch_op.drop_column("imdb_rating")

    with op.batch_alter_table("movies", schema=None) as batch_op:
        batch_op.drop_column("imdb_ratings_refreshed_at")
        batch_op.drop_column("imdb_vote_count")
        batch_op.drop_column("imdb_rating")

    op.drop_table("imdb_title_ratings")
    op.drop_table("imdb_ratings_ingest_state")
