"""add user page access controls

Revision ID: cd8a74b3e901
Revises: bc34de56fa78
Create Date: 2026-06-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd8a74b3e901"
down_revision: str | Sequence[str] | None = "bc34de56fa78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    user_cols = _cols("users")
    if "allowed_pages" not in user_cols:
        with op.batch_alter_table("users", schema=None) as batch_op:
            batch_op.add_column(sa.Column("allowed_pages", sa.JSON(), nullable=True))

    general_cols = _cols("general_settings")
    if "default_allowed_pages" not in general_cols:
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "default_allowed_pages",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[\"candidates\", \"settings\"]'"),
                )
            )


def downgrade() -> None:
    general_cols = _cols("general_settings")
    if "default_allowed_pages" in general_cols:
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("default_allowed_pages")

    user_cols = _cols("users")
    if "allowed_pages" in user_cols:
        with op.batch_alter_table("users", schema=None) as batch_op:
            batch_op.drop_column("allowed_pages")
