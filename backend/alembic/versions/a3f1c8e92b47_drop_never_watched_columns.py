"""drop never_watched columns from movies, series, seasons

Revision ID: a3f1c8e92b47
Revises: 20666951d75d
Create Date: 2026-04-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f1c8e92b47'
down_revision: Union[str, None] = '20666951d75d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # clean up any leftover temp tables from a previous failed run
    for tbl in ('_alembic_tmp_movies', '_alembic_tmp_series', '_alembic_tmp_seasons'):
        conn.execute(sa.text(f'DROP TABLE IF EXISTS "{tbl}"'))

    conn.execute(sa.text('PRAGMA foreign_keys=OFF'))
    try:
        with op.batch_alter_table('movies', schema=None) as batch_op:
            batch_op.drop_column('never_watched')

        with op.batch_alter_table('series', schema=None) as batch_op:
            batch_op.drop_column('never_watched')

        with op.batch_alter_table('seasons', schema=None) as batch_op:
            batch_op.drop_column('never_watched')
    finally:
        conn.execute(sa.text('PRAGMA foreign_keys=ON'))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text('PRAGMA foreign_keys=OFF'))
    try:
        with op.batch_alter_table('seasons', schema=None) as batch_op:
            batch_op.add_column(sa.Column('never_watched', sa.Boolean(), nullable=True))

        with op.batch_alter_table('series', schema=None) as batch_op:
            batch_op.add_column(sa.Column('never_watched', sa.Boolean(), nullable=False, server_default='1'))

        with op.batch_alter_table('movies', schema=None) as batch_op:
            batch_op.add_column(sa.Column('never_watched', sa.Boolean(), nullable=False, server_default='1'))
    finally:
        conn.execute(sa.text('PRAGMA foreign_keys=ON'))