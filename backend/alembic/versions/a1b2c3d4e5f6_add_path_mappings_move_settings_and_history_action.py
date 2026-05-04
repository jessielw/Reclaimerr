"""Add path mappings, move settings, and action field to reclaim history

Revision ID: a1b2c3d4e5f6
Revises: 1201d9f4dfa6
Create Date: 2026-05-15 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "1201d9f4dfa6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("path_mappings", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "move_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("move_destination_root", sa.String(length=1024), nullable=True)
        )

    with op.batch_alter_table("reclaim_history", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "action",
                sa.String(length=20),
                nullable=False,
                server_default="deleted",
            )
        )
        batch_op.add_column(
            sa.Column("destination_path", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("reclaim_history", schema=None) as batch_op:
        batch_op.drop_column("destination_path")
        batch_op.drop_column("action")

    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.drop_column("move_destination_root")
        batch_op.drop_column("move_enabled")
        batch_op.drop_column("path_mappings")
