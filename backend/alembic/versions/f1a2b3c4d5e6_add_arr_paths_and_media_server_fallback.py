"""add arr paths and media server fallback setting

Revision ID: f1a2b3c4d5e6
Revises: e9f1a2b3c4d5
Create Date: 2026-05-11 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e9f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    if "arr_movie_path" not in _cols("movie_arr_refs"):
        with op.batch_alter_table("movie_arr_refs", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("arr_movie_path", sa.String(2048), nullable=True)
            )

    if "arr_series_path" not in _cols("series_arr_refs"):
        with op.batch_alter_table("series_arr_refs", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("arr_series_path", sa.String(2048), nullable=True)
            )

    if "media_server_fallback_enabled" not in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "media_server_fallback_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default="1",
                )
            )


def downgrade() -> None:
    if "arr_movie_path" in _cols("movie_arr_refs"):
        with op.batch_alter_table("movie_arr_refs", schema=None) as batch_op:
            batch_op.drop_column("arr_movie_path")

    if "arr_series_path" in _cols("series_arr_refs"):
        with op.batch_alter_table("series_arr_refs", schema=None) as batch_op:
            batch_op.drop_column("arr_series_path")

    if "media_server_fallback_enabled" in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("media_server_fallback_enabled")
