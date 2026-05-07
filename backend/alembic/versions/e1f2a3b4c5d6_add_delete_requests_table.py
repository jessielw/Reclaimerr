"""add delete requests table

Revision ID: e1f2a3b4c5d6
Revises: f7a9c2e4d1b0
Create Date: 2026-05-05 23:45:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "f7a9c2e4d1b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delete_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "media_type",
            sa.Enum("MOVIE", "SERIES", name="mediatype"),
            nullable=False,
        ),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.Column("movie_version_id", sa.Integer(), nullable=True),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("season_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PENDING", "APPROVED", "DENIED", name="protectionrequeststatus"),
            nullable=False,
        ),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("admin_notes", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("execution_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"]),
        sa.ForeignKeyConstraint(["movie_version_id"], ["movie_versions.id"]),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"]),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_delete_requests_movie_version_id",
        "delete_requests",
        ["movie_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_delete_requests_season_id",
        "delete_requests",
        ["season_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_delete_requests_season_id", table_name="delete_requests")
    op.drop_index("ix_delete_requests_movie_version_id", table_name="delete_requests")
    op.drop_table("delete_requests")
