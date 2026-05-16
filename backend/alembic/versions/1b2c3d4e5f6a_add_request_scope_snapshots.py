"""add request scope snapshots

Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f
Create Date: 2026-05-16 00:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "1b2c3d4e5f6a"
down_revision: Union[str, None] = "0a1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("protection_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("target_scope", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("season_number_snapshot", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("episode_number_snapshot", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("episode_name_snapshot", sa.String(length=500), nullable=True)
        )

    with op.batch_alter_table("delete_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("target_scope", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("season_number_snapshot", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("episode_number_snapshot", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("episode_name_snapshot", sa.String(length=500), nullable=True)
        )

    bind = op.get_bind()
    for table_name in ("protection_requests", "delete_requests"):
        bind.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET
                    target_scope = CASE
                        WHEN media_type = 'movie' AND movie_version_id IS NOT NULL THEN 'movie_version'
                        WHEN media_type = 'movie' THEN 'movie'
                        WHEN episode_id IS NOT NULL THEN 'episode'
                        WHEN season_id IS NOT NULL THEN 'season'
                        ELSE 'series'
                    END,
                    season_number_snapshot = COALESCE(
                        season_number_snapshot,
                        (SELECT seasons.season_number FROM seasons WHERE seasons.id = {table_name}.season_id)
                    ),
                    episode_number_snapshot = COALESCE(
                        episode_number_snapshot,
                        (SELECT episodes.episode_number FROM episodes WHERE episodes.id = {table_name}.episode_id)
                    ),
                    episode_name_snapshot = COALESCE(
                        episode_name_snapshot,
                        (SELECT episodes.name FROM episodes WHERE episodes.id = {table_name}.episode_id)
                    )
                """
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("delete_requests", schema=None) as batch_op:
        batch_op.drop_column("episode_name_snapshot")
        batch_op.drop_column("episode_number_snapshot")
        batch_op.drop_column("season_number_snapshot")
        batch_op.drop_column("target_scope")

    with op.batch_alter_table("protection_requests", schema=None) as batch_op:
        batch_op.drop_column("episode_name_snapshot")
        batch_op.drop_column("episode_number_snapshot")
        batch_op.drop_column("season_number_snapshot")
        batch_op.drop_column("target_scope")
