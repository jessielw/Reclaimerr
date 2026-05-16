"""add default arr delete behavior setting

Revision ID: 2c3d4e5f6a7b
Revises: 1b2c3d4e5f6a
Create Date: 2026-05-16 19:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2c3d4e5f6a7b"
down_revision: Union[str, None] = "1b2c3d4e5f6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    if "default_arr_delete_behavior" not in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "default_arr_delete_behavior",
                    sa.String(length=32),
                    nullable=False,
                    server_default="unmonitor",
                )
            )


def downgrade() -> None:
    if "default_arr_delete_behavior" in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("default_arr_delete_behavior")
