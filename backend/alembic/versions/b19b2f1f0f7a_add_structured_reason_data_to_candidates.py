"""add structured reason data to candidates

Revision ID: b19b2f1f0f7a
Revises: 84e8889cda26
Create Date: 2026-04-30 18:56:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "b19b2f1f0f7a"
down_revision: Union[str, None] = "84e8889cda26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "reclaim_candidates" not in inspector.get_table_names():
        raise RuntimeError(
            "Migration b19b2f1f0f7a expected table 'reclaim_candidates' to exist."
        )

    existing_columns = {
        column["name"] for column in inspector.get_columns("reclaim_candidates")
    }
    if "reason_data" in existing_columns:
        return

    with op.batch_alter_table("reclaim_candidates", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reason_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "reclaim_candidates" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("reclaim_candidates")
    }
    if "reason_data" not in existing_columns:
        return

    with op.batch_alter_table("reclaim_candidates", schema=None) as batch_op:
        batch_op.drop_column("reason_data")
