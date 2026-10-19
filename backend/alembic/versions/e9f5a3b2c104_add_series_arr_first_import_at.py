"""Add earliest known Sonarr episode import date to series.

Revision ID: e9f5a3b2c104
Revises: d8c4e2a1b903
"""

import sqlalchemy as sa
from alembic import op

revision = "e9f5a3b2c104"
down_revision = "d8c4e2a1b903"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("series") as batch:
        batch.add_column(sa.Column("arr_first_import_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("series") as batch:
        batch.drop_column("arr_first_import_at")
