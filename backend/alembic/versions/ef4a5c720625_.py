"""empty message

Revision ID: ef4a5c720625
Revises: d2e4f6a8b0c3, e1f2a3b4c5d6
Create Date: 2026-05-06 11:14:48.130463

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ef4a5c720625'
down_revision: Union[str, None] = ('d2e4f6a8b0c3', 'e1f2a3b4c5d6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
