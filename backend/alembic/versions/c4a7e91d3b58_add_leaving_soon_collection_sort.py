"""Add the Leaving Soon collection sort setting.

Revision ID: c4a7e91d3b58
Revises: d2b6f4a8c135
Create Date: 2026-09-07 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4a7e91d3b58"
down_revision: str | Sequence[str] | None = "d2b6f4a8c135"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_SORT = "default"


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    # existing installs keep today's behavior: Reclaimerr leaves the media
    # server's own collection ordering alone until the user opts in.
    if "leaving_soon_collection_sort" in _cols("general_settings"):
        return
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "leaving_soon_collection_sort",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text(f"'{_DEFAULT_SORT}'"),
            )
        )


def downgrade() -> None:
    if "leaving_soon_collection_sort" not in _cols("general_settings"):
        return
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.drop_column("leaving_soon_collection_sort")
