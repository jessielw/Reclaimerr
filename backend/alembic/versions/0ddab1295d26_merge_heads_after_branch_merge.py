"""Merge heads after branch merge

Revision ID: 0ddab1295d26
Revises: 9930cd1a58b8, a7c3f9d1e210
Create Date: 2026-04-22 19:11:41.721694

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0ddab1295d26'
down_revision: Union[str, None] = ('9930cd1a58b8', 'a7c3f9d1e210')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
