"""Add live playback protection and durable request deferrals.

Revision ID: a6f3d8b2c901
Revises: f7b2e9d4c160
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "a6f3d8b2c901"
down_revision = "f7b2e9d4c160"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("general_settings") as batch:
        batch.add_column(
            sa.Column(
                "active_playback_protection_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    with op.batch_alter_table("reclaim_candidates") as batch:
        batch.add_column(sa.Column("playback_deferral", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("delete_request_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_candidate_delete_request",
            "delete_requests",
            ["delete_request_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_reclaim_candidates_delete_request_id", ["delete_request_id"]
        )
    with op.batch_alter_table("delete_requests") as batch:
        batch.add_column(sa.Column("playback_deferral", sa.JSON(), nullable=True))
    # Preserve request candidates already queued when an installation upgrades.
    # Otherwise a rule scan could discard them before their job runs.
    bind = op.get_bind()
    if "background_jobs" in sa.inspect(bind).get_table_names():
        for (raw,) in bind.execute(
            sa.text(
                "SELECT payload FROM background_jobs WHERE "
                "lower(job_type) = 'candidate_file_op' AND lower(status) IN ('pending', 'running')"
            )
        ):
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
                request_id = payload.get("delete_request_id")
                ids = payload.get("candidate_ids", [])
                if type(request_id) is not int or not isinstance(ids, list):
                    continue
                for candidate_id in ids:
                    if type(candidate_id) is int:
                        bind.execute(
                            sa.text(
                                "UPDATE reclaim_candidates SET delete_request_id = :request "
                                "WHERE id = :candidate AND delete_request_id IS NULL "
                                "AND EXISTS (SELECT 1 FROM delete_requests WHERE id = :request)"
                            ),
                            {"request": request_id, "candidate": candidate_id},
                        )
            except (TypeError, ValueError, AttributeError):
                continue


def downgrade() -> None:
    with op.batch_alter_table("delete_requests") as batch:
        batch.drop_column("playback_deferral")
    with op.batch_alter_table("reclaim_candidates") as batch:
        batch.drop_index("ix_reclaim_candidates_delete_request_id")
        batch.drop_constraint("fk_candidate_delete_request", type_="foreignkey")
        batch.drop_column("delete_request_id")
        batch.drop_column("playback_deferral")
    with op.batch_alter_table("general_settings") as batch:
        batch.drop_column("active_playback_protection_enabled")
