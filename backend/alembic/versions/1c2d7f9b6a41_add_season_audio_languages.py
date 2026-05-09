"""add season audio languages aggregate

Revision ID: 1c2d7f9b6a41
Revises: f7a9c2e4d1b0
Create Date: 2026-04-29 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1c2d7f9b6a41"
down_revision: Union[str, None] = "f7a9c2e4d1b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_seasons"))

    bind = op.get_bind()
    cols = bind.execute(sa.text("PRAGMA table_info(seasons)")).fetchall()
    col_names = {row[1] for row in cols}
    if "audio_languages" not in col_names:
        with op.batch_alter_table("seasons", schema=None) as batch_op:
            batch_op.add_column(sa.Column("audio_languages", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_seasons"))

    bind = op.get_bind()
    cols = bind.execute(sa.text("PRAGMA table_info(seasons)")).fetchall()
    col_names = {row[1] for row in cols}
    if "audio_languages" in col_names:
        with op.batch_alter_table("seasons", schema=None) as batch_op:
            batch_op.drop_column("audio_languages")
