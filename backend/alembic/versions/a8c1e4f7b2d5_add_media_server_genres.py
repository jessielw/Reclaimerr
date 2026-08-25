"""add media server genres

Revision ID: a8c1e4f7b2d5
Revises: f5b8d1e3a7c9
Create Date: 2026-08-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "a8c1e4f7b2d5"
down_revision: str | Sequence[str] | None = "f5b8d1e3a7c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("movie_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("media_server_genres", sa.JSON(), nullable=True))

    with op.batch_alter_table("series_service_refs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("media_server_genres", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("series_service_refs", schema=None) as batch_op:
        batch_op.drop_column("media_server_genres")

    with op.batch_alter_table("movie_versions", schema=None) as batch_op:
        batch_op.drop_column("media_server_genres")
