"""add named service configs

Revision ID: 9d2a6c7f4b10
Revises: 6b0e8c2d4f91
Create Date: 2026-04-29 13:15:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9d2a6c7f4b10"
down_revision: Union[str, None] = "6b0e8c2d4f91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_service_configs"))
    if "name" not in _cols("service_configs"):
        with op.batch_alter_table("service_configs", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("name", sa.String(length=100), nullable=True)
            )

    op.execute(
        sa.text(
            """
            UPDATE service_configs
            SET name = CASE service_type
                WHEN 'radarr' THEN 'Radarr'
                WHEN 'RADARR' THEN 'Radarr'
                WHEN 'sonarr' THEN 'Sonarr'
                WHEN 'SONARR' THEN 'Sonarr'
                WHEN 'seerr' THEN 'Seerr'
                WHEN 'SEERR' THEN 'Seerr'
                WHEN 'plex' THEN 'Plex'
                WHEN 'PLEX' THEN 'Plex'
                WHEN 'jellyfin' THEN 'Jellyfin'
                WHEN 'JELLYFIN' THEN 'Jellyfin'
                WHEN 'emby' THEN 'Emby'
                WHEN 'EMBY' THEN 'Emby'
                ELSE service_type
            END
            WHERE name IS NULL OR TRIM(name) = ''
            """
        )
    )

    # Rebuild explicitly so SQLite drops the previous unique(service_type)
    # constraint from older releases.
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS service_configs_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                service_type VARCHAR(20) NOT NULL,
                name VARCHAR(100) NOT NULL,
                base_url VARCHAR(255) NOT NULL,
                api_key VARCHAR(255) NOT NULL,
                enabled BOOLEAN NOT NULL,
                extra_settings JSON,
                is_main BOOLEAN NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                CONSTRAINT uq_service_config_type_name UNIQUE (service_type, name)
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO service_configs_new (
                id, service_type, name, base_url, api_key, enabled,
                extra_settings, is_main, updated_at
            )
            SELECT id, service_type, name, base_url, api_key, enabled,
                extra_settings, is_main, updated_at
            FROM service_configs
            """
        )
    )
    op.execute(sa.text("DROP TABLE service_configs"))
    op.execute(sa.text("ALTER TABLE service_configs_new RENAME TO service_configs"))
    op.create_index("ix_service_configs_service_type", "service_configs", ["service_type"])


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_service_configs"))
    if "name" in _cols("service_configs"):
        with op.batch_alter_table("service_configs", schema=None) as batch_op:
            batch_op.drop_constraint("uq_service_config_type_name", type_="unique")
            batch_op.drop_index("ix_service_configs_service_type")
            batch_op.drop_column("name")
