"""Add saved storage capacity mount choices.

Revision ID: f1a6c8d3e205
Revises: e9f5a3b2c104
"""

import sqlalchemy as sa
from alembic import op

revision = "f1a6c8d3e205"
down_revision = "e9f5a3b2c104"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("general_settings") as batch:
        batch.add_column(
            sa.Column(
                "storage_mount_overrides", sa.JSON(), nullable=False, server_default="{}"
            )
        )


def downgrade():
    with op.batch_alter_table("general_settings") as batch:
        batch.drop_column("storage_mount_overrides")
