"""add season episode paths

Revision ID: c1d3e5f7a9b2
Revises: a2b4d6e8f0c1
Create Date: 2026-05-03 00:00:00.000000

"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d3e5f7a9b2"
down_revision: Union[str, None] = "a2b4d6e8f0c1"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    with op.batch_alter_table("seasons") as batch_op:
        batch_op.add_column(sa.Column("episode_paths", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("seasons") as batch_op:
        batch_op.drop_column("episode_paths")
