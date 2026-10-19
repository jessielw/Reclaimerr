"""Add movie first successful Radarr import timestamp.

Revision ID: d8c4e2a1b903
Revises: c9e4f7a2b601
"""

import sqlalchemy as sa
from alembic import op

revision = "d8c4e2a1b903"
down_revision = "c9e4f7a2b601"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("movies") as batch:
        batch.add_column(sa.Column("arr_first_import_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("movies") as batch:
        batch.drop_column("arr_first_import_at")
