"""add arr_tags to movies and series

Revision ID: d2e4f6a8b0c3
Revises: c1d3e5f7a9b2
Create Date: 2026-05-04 00:00:00.000000

"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e4f6a8b0c3"
down_revision: Union[str, None] = "c1d3e5f7a9b2"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    with op.batch_alter_table("movies") as batch_op:
        batch_op.add_column(sa.Column("arr_tags", sa.JSON(), nullable=True))

    with op.batch_alter_table("series") as batch_op:
        batch_op.add_column(sa.Column("arr_tags", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("movies") as batch_op:
        batch_op.drop_column("arr_tags")

    with op.batch_alter_table("series") as batch_op:
        batch_op.drop_column("arr_tags")
