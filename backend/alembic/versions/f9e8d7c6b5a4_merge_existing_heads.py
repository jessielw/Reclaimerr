"""merge existing alembic heads

Revision ID: f9e8d7c6b5a4
Revises: c3a4f93e21aa, d2e4f6a8b0c3, d4a8c6b1e2f0, d8e9f0a1b2c3, e1f2a3b4c5d6, e6f7a8b9c0d1, f1a2b3c4d5e6
Create Date: 2026-05-16 00:00:00.000000
"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "f9e8d7c6b5a4"
down_revision: Union[str, None] = (
    "c3a4f93e21aa",
    "d2e4f6a8b0c3",
    "d4a8c6b1e2f0",
    "d8e9f0a1b2c3",
    "e1f2a3b4c5d6",
    "e6f7a8b9c0d1",
    "f1a2b3c4d5e6",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
