"""Allow operation evidence for duplicate files without a cleanup candidate.

Revision ID: b4d9f2a6c831
Revises: a3c8e1f5b920
"""

import sqlalchemy as sa
from alembic import op

revision = "b4d9f2a6c831"
down_revision = "a3c8e1f5b920"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("support_operation_events") as batch:
        batch.alter_column("candidate_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    # Only the new non-candidate diagnostic observations lack a candidate ID.
    op.execute("DELETE FROM support_operation_events WHERE candidate_id IS NULL")
    with op.batch_alter_table("support_operation_events") as batch:
        batch.alter_column("candidate_id", existing_type=sa.Integer(), nullable=False)
