"""set_null_candidate_and_delete_request_version_fks

Add ON DELETE SET NULL to movie_version_id foreign keys on reclaim_candidates
and delete_requests so that pruning stale MovieVersion rows during sync does
not violate the FK constraint.

SQLite does not support ALTER COLUMN so each table is fully recreated via
batch_alter_table(copy_from=..., recreate='always').

Revision ID: d5e6f7a8b9c0
Revises: c8f1a2b3d4e5
Create Date: 2026-05-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c8f1a2b3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _reclaim_candidates_table(version_set_null: bool) -> sa.Table:
    return sa.Table(
        "reclaim_candidates",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("media_type", sa.String(length=6), nullable=False),
        sa.Column("matched_rule_ids", sa.JSON(), nullable=False),
        sa.Column("matched_criteria", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("season_id", sa.Integer(), nullable=True),
        sa.Column("reviewed", sa.Boolean(), nullable=False),
        sa.Column("approved_for_deletion", sa.Boolean(), nullable=False),
        sa.Column("tagged_in_arr", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("movie_version_id", sa.Integer(), nullable=True),
        sa.Column("delete_attempts", sa.Integer(), nullable=False),
        sa.Column("last_delete_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_delete_error", sa.Text(), nullable=True),
        sa.Column("reason_data", sa.JSON(), nullable=True),
        sa.Column("estimated_space_bytes", sa.BigInteger(), nullable=True),
        sa.Column("episode_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["movie_version_id"],
            ["movie_versions.id"],
            name="fk_reclaim_candidates_movie_version_id_movie_versions",
            ondelete="SET NULL" if version_set_null else None,
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name="fk_reclaim_candidates_episode_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"]),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"]),
    )


def _delete_requests_table(version_set_null: bool) -> sa.Table:
    return sa.Table(
        "delete_requests",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("media_type", sa.String(length=6), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.Column("movie_version_id", sa.Integer(), nullable=True),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("season_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("admin_notes", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("execution_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"]),
        sa.ForeignKeyConstraint(
            ["movie_version_id"],
            ["movie_versions.id"],
            ondelete="SET NULL" if version_set_null else None,
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"]),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
    )


def upgrade() -> None:
    with op.batch_alter_table(
        "reclaim_candidates",
        copy_from=_reclaim_candidates_table(version_set_null=True),
        recreate="always",
    ):
        pass

    with op.batch_alter_table(
        "delete_requests",
        copy_from=_delete_requests_table(version_set_null=True),
        recreate="always",
    ):
        pass


def downgrade() -> None:
    with op.batch_alter_table(
        "delete_requests",
        copy_from=_delete_requests_table(version_set_null=False),
        recreate="always",
    ):
        pass

    with op.batch_alter_table(
        "reclaim_candidates",
        copy_from=_reclaim_candidates_table(version_set_null=False),
        recreate="always",
    ):
        pass
