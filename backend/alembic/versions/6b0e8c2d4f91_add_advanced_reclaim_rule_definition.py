"""add advanced reclaim rule definition

Revision ID: 6b0e8c2d4f91
Revises: 1c2d7f9b6a41
Create Date: 2026-04-29 12:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b0e8c2d4f91"
down_revision: Union[str, None] = "1c2d7f9b6a41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    cols = bind.execute(sa.text(f"PRAGMA table_info({table_name})")).fetchall()
    return {row[1] for row in cols}


def upgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_reclaim_rules"))

    existing = _column_names("reclaim_rules")
    missing = {
        "target_scope": sa.Column("target_scope", sa.String(length=32), nullable=True),
        "definition": sa.Column("definition", sa.JSON(), nullable=True),
        "action": sa.Column("action", sa.JSON(), nullable=True),
    }

    columns_to_add = [
        column for name, column in missing.items() if name not in existing
    ]
    if columns_to_add:
        with op.batch_alter_table("reclaim_rules", schema=None) as batch_op:
            for column in columns_to_add:
                batch_op.add_column(column)


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_reclaim_rules"))

    existing = _column_names("reclaim_rules")
    columns_to_drop = [
        name
        for name in ("action", "definition", "target_scope")
        if name in existing
    ]
    if columns_to_drop:
        with op.batch_alter_table("reclaim_rules", schema=None) as batch_op:
            for column_name in columns_to_drop:
                batch_op.drop_column(column_name)
