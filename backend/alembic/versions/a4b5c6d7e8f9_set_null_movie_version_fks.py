"""set_null_movie_version_fks

Add ON DELETE SET NULL to movie_version_id foreign keys on protected_media
and protection_requests so that per-version protection degrades gracefully
to whole-movie protection when a version is removed from the media library.

Revision ID: a4b5c6d7e8f9
Revises: ef4a5c720625
Create Date: 2026-05-06 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, None] = "ef4a5c720625"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite does not support ALTER COLUMN, so batch_alter_table recreates the table.
    # Drop the existing FK constraint and recreate it with ON DELETE SET NULL.

    with op.batch_alter_table("protected_media", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_protected_media_movie_version_id_movie_versions",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_protected_media_movie_version_id_movie_versions",
            "movie_versions",
            ["movie_version_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("protection_requests", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_protection_requests_movie_version_id_movie_versions",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_protection_requests_movie_version_id_movie_versions",
            "movie_versions",
            ["movie_version_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("protection_requests", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_protection_requests_movie_version_id_movie_versions",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_protection_requests_movie_version_id_movie_versions",
            "movie_versions",
            ["movie_version_id"],
            ["id"],
        )

    with op.batch_alter_table("protected_media", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_protected_media_movie_version_id_movie_versions",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_protected_media_movie_version_id_movie_versions",
            "movie_versions",
            ["movie_version_id"],
            ["id"],
        )
