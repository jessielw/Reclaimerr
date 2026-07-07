"""Add Sonarr-known episode inventory to seasons.

Revision ID: d4a7e2c9f1b6
Revises: c8f3a1d6e2b9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a7e2c9f1b6"
down_revision: str | Sequence[str] | None = "c8f3a1d6e2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("seasons") as batch_op:
        batch_op.add_column(
            sa.Column("sonarr_episode_numbers", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("seasons") as batch_op:
        batch_op.drop_column("sonarr_episode_numbers")
