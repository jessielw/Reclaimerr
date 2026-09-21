"""Add the update-available notification toggle.

Revision ID: c4e7b0a9d316
Revises: b3d9c1e8f4a2
Create Date: 2026-09-21 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e7b0a9d316"
down_revision: str | Sequence[str] | None = "b3d9c1e8f4a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    # Off for everyone on upgrade: an admin opts in per destination, the same
    # way every other notification type starts out.
    if "update_available" not in _cols("notification_settings"):
        with op.batch_alter_table("notification_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "update_available",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )


def downgrade() -> None:
    if "update_available" in _cols("notification_settings"):
        with op.batch_alter_table("notification_settings", schema=None) as batch_op:
            batch_op.drop_column("update_available")
