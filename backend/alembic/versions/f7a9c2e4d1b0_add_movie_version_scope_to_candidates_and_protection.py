"""add movie version scope to candidates and protection

Revision ID: f7a9c2e4d1b0
Revises: b4f3d2c1a7de
Create Date: 2026-04-27 01:20:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a9c2e4d1b0"
down_revision: Union[str, None] = "b4f3d2c1a7de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # cleanup from interrupted SQLite batch migrations (we need to be sure retries are safe)
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_reclaim_candidates"))
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_protected_media"))
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_protection_requests"))

    with op.batch_alter_table("reclaim_candidates", schema=None) as batch_op:
        batch_op.add_column(sa.Column("movie_version_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "delete_attempts",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("last_delete_attempt_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(sa.Column("last_delete_error", sa.Text(), nullable=True))
        batch_op.create_index(
            "ix_reclaim_candidates_movie_version_id", ["movie_version_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_reclaim_candidates_movie_version_id_movie_versions",
            "movie_versions",
            ["movie_version_id"],
            ["id"],
        )
    # backfill before enforcing NOT NULL (important for SQLite batch migrations)
    op.execute(
        sa.text(
            "UPDATE reclaim_candidates "
            "SET delete_attempts = 0 "
            "WHERE delete_attempts IS NULL"
        )
    )
    with op.batch_alter_table("reclaim_candidates", schema=None) as batch_op:
        batch_op.alter_column("delete_attempts", nullable=False)

    with op.batch_alter_table("protected_media", schema=None) as batch_op:
        batch_op.add_column(sa.Column("movie_version_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_protected_media_movie_version_id", ["movie_version_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_protected_media_movie_version_id_movie_versions",
            "movie_versions",
            ["movie_version_id"],
            ["id"],
        )

    with op.batch_alter_table("protection_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("movie_version_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_protection_requests_movie_version_id",
            ["movie_version_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_protection_requests_movie_version_id_movie_versions",
            "movie_versions",
            ["movie_version_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("protection_requests", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_protection_requests_movie_version_id_movie_versions",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_protection_requests_movie_version_id")
        batch_op.drop_column("movie_version_id")

    with op.batch_alter_table("protected_media", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_protected_media_movie_version_id_movie_versions",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_protected_media_movie_version_id")
        batch_op.drop_column("movie_version_id")

    with op.batch_alter_table("reclaim_candidates", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_reclaim_candidates_movie_version_id_movie_versions",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_reclaim_candidates_movie_version_id")
        batch_op.drop_column("last_delete_error")
        batch_op.drop_column("last_delete_attempt_at")
        batch_op.drop_column("delete_attempts")
        batch_op.drop_column("movie_version_id")
