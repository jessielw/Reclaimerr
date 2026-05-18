"""add candidate file op background job type

Revision ID: 3d4e5f6a7b8c
Revises: 2c3d4e5f6a7b
Create Date: 2026-05-17 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "3d4e5f6a7b8c"
down_revision: Union[str, None] = "2c3d4e5f6a7b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_JOB_TYPE = sa.Enum(
    "SERVICE_TOGGLE",
    "TASK_RUN",
    name="backgroundjobtype",
)
_NEW_JOB_TYPE = sa.Enum(
    "SERVICE_TOGGLE",
    "TASK_RUN",
    "CANDIDATE_FILE_OP",
    name="backgroundjobtype",
)


def upgrade() -> None:
    with op.batch_alter_table("background_jobs", schema=None) as batch_op:
        batch_op.alter_column(
            "job_type",
            existing_type=_OLD_JOB_TYPE,
            type_=_NEW_JOB_TYPE,
            existing_nullable=False,
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM background_jobs WHERE job_type = 'CANDIDATE_FILE_OP'"
        )
    )
    with op.batch_alter_table("background_jobs", schema=None) as batch_op:
        batch_op.alter_column(
            "job_type",
            existing_type=_NEW_JOB_TYPE,
            type_=_OLD_JOB_TYPE,
            existing_nullable=False,
        )
