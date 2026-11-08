"""Track which notification destination URL an admin approved.

Revision ID: a3c8e1f5b920
Revises: f7b2d4e9a651
"""

import sqlalchemy as sa
from alembic import op

revision = "a3c8e1f5b920"
down_revision = "f7b2d4e9a651"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("notification_settings")
    }
    if "approved_url_hash" in columns:
        return
    with op.batch_alter_table("notification_settings") as batch_op:
        batch_op.add_column(sa.Column("approved_url_hash", sa.String(64)))


def downgrade():
    with op.batch_alter_table("notification_settings") as batch_op:
        batch_op.drop_column("approved_url_hash")
