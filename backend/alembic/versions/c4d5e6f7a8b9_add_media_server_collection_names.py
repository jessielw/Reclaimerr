"""add media server collection names

Revision ID: c4d5e6f7a8b9
Revises: b1a2c3d4e5f6
Create Date: 2026-06-12 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "b1a2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("movie_versions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("media_server_collection_names", sa.JSON(), nullable=True)
        )

    with op.batch_alter_table("series_service_refs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("media_server_collection_names", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("series_service_refs", schema=None) as batch_op:
        batch_op.drop_column("media_server_collection_names")

    with op.batch_alter_table("movie_versions", schema=None) as batch_op:
        batch_op.drop_column("media_server_collection_names")
