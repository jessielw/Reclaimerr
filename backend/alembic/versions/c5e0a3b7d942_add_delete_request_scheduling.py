"""Add grace periods and link scheduled deletions to approved requests.

Revision ID: c5e0a3b7d942
Revises: b4d9f2a6c831
"""

import sqlalchemy as sa
from alembic import op

revision = "c5e0a3b7d942"
down_revision = "b4d9f2a6c831"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "general_settings",
        sa.Column(
            "delete_request_grace_period_days",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    with op.batch_alter_table("scheduled_deletions") as batch:
        batch.add_column(sa.Column("delete_request_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("requester_warned_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            "fk_scheduled_deletions_delete_request_id",
            "delete_requests",
            ["delete_request_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            "uq_scheduled_deletions_delete_request_id", ["delete_request_id"]
        )


def downgrade():
    with op.batch_alter_table("scheduled_deletions") as batch:
        batch.drop_constraint(
            "uq_scheduled_deletions_delete_request_id", type_="unique"
        )
        batch.drop_constraint(
            "fk_scheduled_deletions_delete_request_id", type_="foreignkey"
        )
        batch.drop_column("requester_warned_at")
        batch.drop_column("delete_request_id")
    with op.batch_alter_table("general_settings") as batch:
        batch.drop_column("delete_request_grace_period_days")
