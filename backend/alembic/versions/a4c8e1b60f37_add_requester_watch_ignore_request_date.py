"""add requester_watch_ignore_request_date to general settings

`seerr.requester_watched_after_request` compares each play against the
requester's earliest request for the season. That comparison is only as good as
the request dates: a Seerr that was rebuilt, migrated between instances, or
simply re-requested writes rows dated after the plays they describe, and the
field then reads false for an entire library no matter how the identity join
resolves.

The switch drops the date half of that field, leaving the completion half. It
defaults off, so no existing install changes what it matches.

Revision ID: a4c8e1b60f37
Revises: e3f7a1c85d92
Create Date: 2026-08-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c8e1b60f37"
down_revision: str | Sequence[str] | None = "e3f7a1c85d92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "requester_watch_ignore_request_date"


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    if COLUMN in _cols("general_settings"):
        return
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    if COLUMN not in _cols("general_settings"):
        return
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.drop_column(COLUMN)
