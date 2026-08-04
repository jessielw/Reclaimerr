"""add playback min seconds settings

Revision ID: a3f6b8d1c4e2
Revises: d7f9a2c4e6b8
Create Date: 2026-08-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f6b8d1c4e2"
down_revision: str | Sequence[str] | None = "d7f9a2c4e6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "playback_movie_min_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("15"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "playback_episode_min_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("7"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.drop_column("playback_episode_min_seconds")
        batch_op.drop_column("playback_movie_min_seconds")
