"""Add external candidate API lifecycle and durable webhooks.

Revision ID: aa17c9d4e6f2
Revises: c0d1e2f3a4b
Create Date: 2026-07-17 00:00:00.000000
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from backend.core.encryption import fer_decrypt, fer_encrypt


revision = "aa17c9d4e6f2"
down_revision = "c0d1e2f3a4b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_prefix", sa.String(length=24), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("token_prefix"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_api_tokens_token_prefix", "api_tokens", ["token_prefix"])
    op.create_index(
        "ix_api_tokens_created_by_user_id", "api_tokens", ["created_by_user_id"]
    )

    with op.batch_alter_table("reclaim_candidates") as batch_op:
        batch_op.add_column(sa.Column("auto_delete_cancelled_at", sa.DateTime()))
        batch_op.add_column(sa.Column("auto_delete_timer_started_at", sa.DateTime()))
        batch_op.add_column(sa.Column("auto_delete_postponed_until", sa.DateTime()))
        batch_op.add_column(sa.Column("lifecycle_reason", sa.Text()))
        batch_op.add_column(sa.Column("lifecycle_updated_at", sa.DateTime()))
        batch_op.add_column(
            sa.Column("lifecycle_updated_by_api_token_id", sa.Integer())
        )
        batch_op.add_column(sa.Column("auto_delete_announced_at", sa.DateTime()))
        batch_op.create_foreign_key(
            "fk_reclaim_candidates_lifecycle_api_token",
            "api_tokens",
            ["lifecycle_updated_by_api_token_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_reclaim_candidates_lifecycle_api_token",
            ["lifecycle_updated_by_api_token_id"],
        )

    with op.batch_alter_table("protected_media") as batch_op:
        batch_op.add_column(sa.Column("protected_by_api_token_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_protected_media_api_token",
            "api_tokens",
            ["protected_by_api_token_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_protected_media_api_token", ["protected_by_api_token_id"]
        )

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("url_template", sa.String(length=2048), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("method", sa.String(length=8), nullable=False, server_default="POST"),
        sa.Column("event_types", sa.JSON(), nullable=False),
        sa.Column("media_types", sa.JSON(), nullable=False),
        sa.Column("path_mode", sa.String(length=16), nullable=False, server_default="original"),
        sa.Column("body_template", sa.Text()),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("auth_username", sa.String(length=255)),
        sa.Column("auth_password_encrypted", sa.Text()),
        sa.Column("headers_encrypted", sa.Text()),
        sa.Column("deleted_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "lifecycle_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("candidate_id", sa.Integer()),
        sa.Column("actor_api_token_id", sa.Integer()),
        sa.Column("actor_user_id", sa.Integer()),
        sa.Column("idempotency_key", sa.String(length=120)),
        sa.Column("response_payload", sa.JSON()),
        sa.Column("occurred_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["reclaim_candidates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["actor_api_token_id"], ["api_tokens.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("event_id"),
        sa.UniqueConstraint(
            "actor_api_token_id",
            "idempotency_key",
            name="uq_lifecycle_event_token_idempotency",
        ),
    )
    op.create_index("ix_lifecycle_events_event_id", "lifecycle_events", ["event_id"])
    op.create_index("ix_lifecycle_events_event_type", "lifecycle_events", ["event_type"])
    op.create_index("ix_lifecycle_events_candidate_id", "lifecycle_events", ["candidate_id"])
    op.create_index("ix_lifecycle_events_actor_api_token_id", "lifecycle_events", ["actor_api_token_id"])
    op.create_index("ix_lifecycle_events_actor_user_id", "lifecycle_events", ["actor_user_id"])
    op.create_index("ix_lifecycle_events_occurred_at", "lifecycle_events", ["occurred_at"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_status_code", sa.Integer()),
        sa.Column("last_error", sa.Text()),
        sa.Column("delivered_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["event_id"], ["lifecycle_events.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["webhook_endpoints.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "event_id", "endpoint_id", name="uq_webhook_delivery_event_endpoint"
        ),
    )
    op.create_index("ix_webhook_deliveries_event_id", "webhook_deliveries", ["event_id"])
    op.create_index("ix_webhook_deliveries_endpoint_id", "webhook_deliveries", ["endpoint_id"])
    op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])
    op.create_index("ix_webhook_deliveries_next_attempt_at", "webhook_deliveries", ["next_attempt_at"])

    _migrate_legacy_webhooks()
    with op.batch_alter_table("general_settings") as batch_op:
        batch_op.drop_column("post_action_webhooks")


def _migrate_legacy_webhooks() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT post_action_webhooks FROM general_settings LIMIT 1")
    ).fetchall()
    if not rows or not rows[0][0]:
        return
    raw = rows[0][0]
    configs = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(configs, list):
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    table = sa.table(
        "webhook_endpoints",
        sa.column("name"),
        sa.column("url_template"),
        sa.column("enabled"),
        sa.column("method"),
        sa.column("event_types"),
        sa.column("media_types"),
        sa.column("path_mode"),
        sa.column("body_template"),
        sa.column("timeout_seconds"),
        sa.column("auth_username"),
        sa.column("auth_password_encrypted"),
        sa.column("headers_encrypted"),
        sa.column("created_at"),
        sa.column("updated_at"),
    )
    for config in configs:
        if not isinstance(config, dict) or not config.get("url_template"):
            continue
        actions = [
            f"candidate.{action}"
            for action in config.get("actions", ["deleted", "moved"])
            if action in {"deleted", "moved"}
        ]
        headers = config.get("headers") or []
        password = config.get("auth_password")
        bind.execute(
            table.insert().values(
                name=str(config.get("name") or "Post-action webhook"),
                url_template=str(config["url_template"]),
                enabled=bool(config.get("enabled", True)),
                method=str(config.get("method") or "GET").upper(),
                event_types=actions,
                media_types=config.get("media_types") or ["movie", "series"],
                path_mode=str(config.get("path_mode") or "original"),
                body_template=config.get("body_template"),
                timeout_seconds=int(config.get("timeout_seconds") or 15),
                auth_username=config.get("auth_username"),
                auth_password_encrypted=fer_encrypt(str(password)) if password else None,
                headers_encrypted=fer_encrypt(json.dumps(headers)) if headers else None,
                created_at=now,
                updated_at=now,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("general_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "post_action_webhooks",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
    _restore_legacy_webhooks()
    op.drop_table("webhook_deliveries")
    op.drop_table("lifecycle_events")
    op.drop_table("webhook_endpoints")
    with op.batch_alter_table("protected_media") as batch_op:
        batch_op.drop_index("ix_protected_media_api_token")
        batch_op.drop_constraint("fk_protected_media_api_token", type_="foreignkey")
        batch_op.drop_column("protected_by_api_token_id")
    with op.batch_alter_table("reclaim_candidates") as batch_op:
        batch_op.drop_index("ix_reclaim_candidates_lifecycle_api_token")
        batch_op.drop_constraint(
            "fk_reclaim_candidates_lifecycle_api_token", type_="foreignkey"
        )
        batch_op.drop_column("auto_delete_announced_at")
        batch_op.drop_column("lifecycle_updated_by_api_token_id")
        batch_op.drop_column("lifecycle_updated_at")
        batch_op.drop_column("lifecycle_reason")
        batch_op.drop_column("auto_delete_postponed_until")
        batch_op.drop_column("auto_delete_timer_started_at")
        batch_op.drop_column("auto_delete_cancelled_at")
    op.drop_table("api_tokens")


def _restore_legacy_webhooks() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT name, url_template, enabled, method, event_types, media_types, "
            "path_mode, body_template, timeout_seconds, auth_username, "
            "auth_password_encrypted, headers_encrypted FROM webhook_endpoints "
            "WHERE deleted_at IS NULL ORDER BY id"
        )
    ).mappings()
    configs: list[dict[str, object]] = []
    for row in rows:
        event_types = row["event_types"]
        if isinstance(event_types, str):
            event_types = json.loads(event_types)
        media_types = row["media_types"]
        if isinstance(media_types, str):
            media_types = json.loads(media_types)
        password = row["auth_password_encrypted"]
        headers = row["headers_encrypted"]
        configs.append(
            {
                "name": row["name"],
                "url_template": row["url_template"],
                "enabled": bool(row["enabled"]),
                "method": row["method"],
                "actions": [
                    str(event_type).removeprefix("candidate.")
                    for event_type in event_types or []
                    if event_type in {"candidate.deleted", "candidate.moved"}
                ],
                "media_types": media_types or ["movie", "series"],
                "path_mode": row["path_mode"],
                "body_template": row["body_template"],
                "timeout_seconds": row["timeout_seconds"],
                "auth_username": row["auth_username"],
                "auth_password": fer_decrypt(password) if password else None,
                "headers": json.loads(fer_decrypt(headers)) if headers else [],
            }
        )
    bind.execute(
        sa.text("UPDATE general_settings SET post_action_webhooks = :configs"),
        {"configs": json.dumps(configs)},
    )
