"""add playback usernames

Revision ID: 9e5b7c3a1d42
Revises: 8c4e6a2d1f30
Create Date: 2026-07-01 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9e5b7c3a1d42"
down_revision: str | Sequence[str] | None = "8c4e6a2d1f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("playback_history_events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source_username", sa.String(length=255), nullable=True)
        )
        batch_op.create_index(
            "ix_playback_history_events_source_username",
            ["source_username"],
            unique=False,
        )

    with op.batch_alter_table(
        "playback_history_aggregates", schema=None
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "usernames",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "usernames_complete",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "playback_history_aggregates", schema=None
    ) as batch_op:
        batch_op.drop_column("usernames_complete")
        batch_op.drop_column("usernames")

    with op.batch_alter_table("playback_history_events", schema=None) as batch_op:
        batch_op.drop_index("ix_playback_history_events_source_username")
        batch_op.drop_column("source_username")
