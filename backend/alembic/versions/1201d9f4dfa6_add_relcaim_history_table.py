"""Add re-claim history table

Revision ID: 1201d9f4dfa6
Revises: d4a8c6b1e2f0
Create Date: 2026-05-02 16:06:06.838578

"""
from typing import Sequence, Union
from sqlalchemy import inspect

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1201d9f4dfa6'
down_revision: Union[str, None] = 'd4a8c6b1e2f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'reclaim_history' in inspector.get_table_names():
        return

    op.create_table('reclaim_history',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('approved_by', sa.String(length=32), nullable=False),
        sa.Column('media_type', sa.Enum('MOVIE', 'SERIES', name='mediatype'), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=True),
        sa.Column('path', sa.String(length=1024), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('reclaim_history', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_reclaim_history_tmdb_id'), ['tmdb_id'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'reclaim_history' not in inspector.get_table_names():
        return

    with op.batch_alter_table('reclaim_history', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reclaim_history_tmdb_id'))
    op.drop_table('reclaim_history')
