"""Add bounded support decision and operation history.

Revision ID: c4e9a1b6d328
Revises: b3d8f0a5c217
"""

import sqlalchemy as sa
from alembic import op

revision = "c4e9a1b6d328"
down_revision = "b3d8f0a5c217"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "support_scan_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.Column("job_id", sa.Integer()),
        sa.Column("dropped_traces", sa.Integer(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_support_scan_runs_started_at", "support_scan_runs", ["started_at"]
    )
    op.create_table(
        "support_decision_traces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scan_id",
            sa.String(36),
            sa.ForeignKey("support_scan_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_scope", sa.String(24), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("rule_revision", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *[
            sa.Column(name, sa.Integer())
            for name in ("movie_id", "series_id", "season_id", "candidate_id")
        ],
    )
    op.create_index(
        "ix_support_trace_target",
        "support_decision_traces",
        ["target_scope", "target_id"],
    )
    for name in (
        "scan_id",
        "observed_at",
        "movie_id",
        "series_id",
        "season_id",
        "candidate_id",
    ):
        op.create_index(
            "ix_support_decision_traces_" + name, "support_decision_traces", [name]
        )
    op.create_table(
        "support_operation_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("target_scope", sa.String(24), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        *[
            sa.Column(name, sa.Integer())
            for name in ("movie_id", "series_id", "season_id", "job_id")
        ],
        sa.Column("error_category", sa.String(48)),
    )
    op.create_index(
        "ix_support_event_target",
        "support_operation_events",
        ["target_scope", "target_id"],
    )
    for name in (
        "correlation_id",
        "occurred_at",
        "candidate_id",
        "movie_id",
        "series_id",
        "season_id",
    ):
        op.create_index(
            "ix_support_operation_events_" + name, "support_operation_events", [name]
        )


def downgrade():
    op.drop_table("support_operation_events")
    op.drop_table("support_decision_traces")
    op.drop_table("support_scan_runs")
