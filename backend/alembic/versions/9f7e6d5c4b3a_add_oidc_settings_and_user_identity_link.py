"""Add OIDC settings table and user identity link columns.

Revision ID: 9f7e6d5c4b3a
Revises: 7d9e1a2b3c4d
Create Date: 2026-05-27 10:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f7e6d5c4b3a"
down_revision: str | Sequence[str] | None = "7d9e1a2b3c4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name LIMIT 1"
        ),
        {"name": table},
    ).first()
    return row is not None


def _index_exists(index: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :name LIMIT 1"
        ),
        {"name": index},
    ).first()
    return row is not None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    if not _table_exists("oidc_settings"):
        op.create_table(
            "oidc_settings",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "issuer_url",
                sa.String(length=500),
                nullable=False,
                server_default=sa.text("''"),
            ),
            sa.Column(
                "client_id",
                sa.String(length=255),
                nullable=False,
                server_default=sa.text("''"),
            ),
            sa.Column(
                "client_secret",
                sa.Text(),
                nullable=False,
                server_default=sa.text("''"),
            ),
            sa.Column(
                "scopes",
                sa.String(length=255),
                nullable=False,
                server_default=sa.text("'openid profile email'"),
            ),
            sa.Column(
                "email_claim",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'email'"),
            ),
            sa.Column(
                "token_endpoint_auth_method",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'client_secret_basic'"),
            ),
            sa.Column("redirect_uri_override", sa.String(length=500), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(
                ["updated_by_user_id"],
                ["users.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    else:
        oidc_cols = _cols("oidc_settings")
        with op.batch_alter_table("oidc_settings", schema=None) as batch_op:
            if "enabled" not in oidc_cols:
                batch_op.add_column(
                    sa.Column(
                        "enabled",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.text("0"),
                    )
                )
            if "issuer_url" not in oidc_cols:
                batch_op.add_column(
                    sa.Column(
                        "issuer_url",
                        sa.String(length=500),
                        nullable=False,
                        server_default=sa.text("''"),
                    )
                )
            if "client_id" not in oidc_cols:
                batch_op.add_column(
                    sa.Column(
                        "client_id",
                        sa.String(length=255),
                        nullable=False,
                        server_default=sa.text("''"),
                    )
                )
            if "client_secret" not in oidc_cols:
                batch_op.add_column(
                    sa.Column(
                        "client_secret",
                        sa.Text(),
                        nullable=False,
                        server_default=sa.text("''"),
                    )
                )
            if "scopes" not in oidc_cols:
                batch_op.add_column(
                    sa.Column(
                        "scopes",
                        sa.String(length=255),
                        nullable=False,
                        server_default=sa.text("'openid profile email'"),
                    )
                )
            if "email_claim" not in oidc_cols:
                batch_op.add_column(
                    sa.Column(
                        "email_claim",
                        sa.String(length=64),
                        nullable=False,
                        server_default=sa.text("'email'"),
                    )
                )
            if "token_endpoint_auth_method" not in oidc_cols:
                batch_op.add_column(
                    sa.Column(
                        "token_endpoint_auth_method",
                        sa.String(length=32),
                        nullable=False,
                        server_default=sa.text("'client_secret_basic'"),
                    )
                )
            if "redirect_uri_override" not in oidc_cols:
                batch_op.add_column(
                    sa.Column(
                        "redirect_uri_override",
                        sa.String(length=500),
                        nullable=True,
                    )
                )
            if "updated_at" not in oidc_cols:
                batch_op.add_column(
                    sa.Column(
                        "updated_at",
                        sa.DateTime(),
                        nullable=False,
                        server_default=sa.func.now(),
                    )
                )
            if "updated_by_user_id" not in oidc_cols:
                batch_op.add_column(
                    sa.Column("updated_by_user_id", sa.Integer(), nullable=True)
                )

    cols = _cols("users")
    with op.batch_alter_table("users", schema=None) as batch_op:
        if "oidc_issuer" not in cols:
            batch_op.add_column(sa.Column("oidc_issuer", sa.String(length=255)))
        if "oidc_subject" not in cols:
            batch_op.add_column(sa.Column("oidc_subject", sa.String(length=255)))

    if not _index_exists("ix_users_oidc_issuer"):
        op.create_index(
            "ix_users_oidc_issuer",
            "users",
            ["oidc_issuer"],
            unique=False,
        )
    if not _index_exists("ix_users_oidc_subject"):
        op.create_index(
            "ix_users_oidc_subject",
            "users",
            ["oidc_subject"],
            unique=False,
        )
    if not _index_exists("uq_users_oidc_identity"):
        op.create_index(
            "uq_users_oidc_identity",
            "users",
            ["oidc_issuer", "oidc_subject"],
            unique=True,
        )


def downgrade() -> None:
    if _index_exists("uq_users_oidc_identity"):
        op.drop_index("uq_users_oidc_identity", table_name="users")
    if _index_exists("ix_users_oidc_subject"):
        op.drop_index("ix_users_oidc_subject", table_name="users")
    if _index_exists("ix_users_oidc_issuer"):
        op.drop_index("ix_users_oidc_issuer", table_name="users")

    cols = _cols("users")
    with op.batch_alter_table("users", schema=None) as batch_op:
        if "oidc_subject" in cols:
            batch_op.drop_column("oidc_subject")
        if "oidc_issuer" in cols:
            batch_op.drop_column("oidc_issuer")

    if _table_exists("oidc_settings"):
        op.drop_table("oidc_settings")
