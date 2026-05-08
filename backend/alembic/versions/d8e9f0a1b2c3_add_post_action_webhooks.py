"""add post action webhooks

Revision ID: d8e9f0a1b2c3
Revises: b6e1d9a4c2f3
Create Date: 2026-05-08 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, None] = "b6e1d9a4c2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    if "post_action_webhooks" not in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "post_action_webhooks",
                    sa.JSON(),
                    nullable=False,
                    server_default="[]",
                )
            )


def downgrade() -> None:
    if "post_action_webhooks" in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("post_action_webhooks")
