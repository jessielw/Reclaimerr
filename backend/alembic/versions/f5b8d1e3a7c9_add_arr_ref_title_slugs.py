"""add Arr title slugs for UI deep links

Revision ID: f5b8d1e3a7c9
Revises: e4a7c9d2f6b1
Create Date: 2026-08-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5b8d1e3a7c9"
down_revision: str | Sequence[str] | None = "e4a7c9d2f6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("movie_arr_refs") as batch_op:
        batch_op.add_column(sa.Column("arr_title_slug", sa.String(255), nullable=True))
    with op.batch_alter_table("series_arr_refs") as batch_op:
        batch_op.add_column(sa.Column("arr_title_slug", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("series_arr_refs") as batch_op:
        batch_op.drop_column("arr_title_slug")
    with op.batch_alter_table("movie_arr_refs") as batch_op:
        batch_op.drop_column("arr_title_slug")
