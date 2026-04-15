"""nullable_year

Revision ID: 1b25d7fd62d3
Revises: e85082c4be59
Create Date: 2026-04-14 19:05:11.724739

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1b25d7fd62d3'
down_revision: Union[str, None] = 'e85082c4be59'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    # drop any orphaned Alembic temp tables left by a previously interrupted migration.
    for tbl in ('_alembic_tmp_movies', '_alembic_tmp_series'):
        conn.execute(sa.text(f'DROP TABLE IF EXISTS "{tbl}"'))

    # SQLite batch_alter_table recreates the table (copy → drop original → rename).
    # The DROP TABLE fails if FK enforcement is on because child tables reference it.
    conn.execute(sa.text('PRAGMA foreign_keys=OFF'))
    try:
        with op.batch_alter_table('movies', schema=None) as batch_op:
            batch_op.alter_column('year',
                   existing_type=sa.SMALLINT(),
                   nullable=True)

        with op.batch_alter_table('series', schema=None) as batch_op:
            batch_op.alter_column('year',
                   existing_type=sa.SMALLINT(),
                   nullable=True)
    finally:
        conn.execute(sa.text('PRAGMA foreign_keys=ON'))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text('PRAGMA foreign_keys=OFF'))
    try:
        with op.batch_alter_table('series', schema=None) as batch_op:
            batch_op.alter_column('year',
                   existing_type=sa.SMALLINT(),
                   nullable=False)

        with op.batch_alter_table('movies', schema=None) as batch_op:
            batch_op.alter_column('year',
                   existing_type=sa.SMALLINT(),
                   nullable=False)
    finally:
        conn.execute(sa.text('PRAGMA foreign_keys=ON'))
