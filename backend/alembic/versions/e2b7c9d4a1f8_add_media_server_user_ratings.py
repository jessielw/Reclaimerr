"""add media server user ratings

Revision ID: e2b7c9d4a1f8
Revises: d4a7e2c9f1b6
Create Date: 2026-07-08 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7c9d4a1f8"
down_revision: str | Sequence[str] | None = "d4a7e2c9f1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns(table)}


def _add_rating_column(table: str) -> None:
    if "media_server_user_rating" in _cols(table):
        return
    with op.batch_alter_table(table) as batch_op:
        batch_op.add_column(sa.Column("media_server_user_rating", sa.Float()))


def _drop_rating_column(table: str) -> None:
    if "media_server_user_rating" not in _cols(table):
        return
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_column("media_server_user_rating")


def upgrade() -> None:
    for table in ("movies", "movie_versions", "series", "seasons", "episodes"):
        _add_rating_column(table)


def downgrade() -> None:
    for table in ("episodes", "seasons", "series", "movie_versions", "movies"):
        _drop_rating_column(table)
