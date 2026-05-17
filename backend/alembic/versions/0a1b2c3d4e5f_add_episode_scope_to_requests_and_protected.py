"""add episode scope to requests and protected media

Revision ID: 0a1b2c3d4e5f
Revises: f9e8d7c6b5a4
Create Date: 2026-05-16 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0a1b2c3d4e5f"
down_revision: Union[str, None] = "f9e8d7c6b5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("protected_media", schema=None) as batch_op:
        batch_op.add_column(sa.Column("episode_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_protected_media_episode_id"), ["episode_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_protected_media_episode_id_episodes",
            "episodes",
            ["episode_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("protection_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("episode_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_protection_requests_episode_id"),
            ["episode_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_protection_requests_episode_id_episodes",
            "episodes",
            ["episode_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("delete_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("episode_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_delete_requests_episode_id"), ["episode_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_delete_requests_episode_id_episodes",
            "episodes",
            ["episode_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("delete_requests", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_delete_requests_episode_id_episodes", type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_delete_requests_episode_id"))
        batch_op.drop_column("episode_id")

    with op.batch_alter_table("protection_requests", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_protection_requests_episode_id_episodes", type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_protection_requests_episode_id"))
        batch_op.drop_column("episode_id")

    with op.batch_alter_table("protected_media", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_protected_media_episode_id_episodes", type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_protected_media_episode_id"))
        batch_op.drop_column("episode_id")
