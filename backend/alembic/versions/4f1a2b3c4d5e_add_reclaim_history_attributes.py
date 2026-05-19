"""add reclaim history attributes

Revision ID: 4f1a2b3c4d5e
Revises: 3d4e5f6a7b8c
Create Date: 2026-05-18 13:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f1a2b3c4d5e"
down_revision: str | Sequence[str] | None = "3d4e5f6a7b8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reclaim_history", schema=None) as batch_op:
        batch_op.add_column(sa.Column("attributes", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reclaim_history", schema=None) as batch_op:
        batch_op.drop_column("attributes")
