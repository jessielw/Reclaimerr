"""add arr-derived file-added dates

Revision ID: 7b3d5f9a1c2e
Revises: 5a8c2e7d9f10
Create Date: 2026-06-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7b3d5f9a1c2e"
down_revision: str | Sequence[str] | None = "5a8c2e7d9f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("movies", "movie_versions", "series", "seasons", "episodes")


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("arr_added_at", sa.DateTime(), nullable=True)
            )


def downgrade() -> None:
    for table in reversed(_TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("arr_added_at")
