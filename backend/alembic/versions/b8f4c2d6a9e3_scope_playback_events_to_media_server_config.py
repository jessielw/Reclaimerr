"""scope playback events to the media server config they were observed on

Adds observed_service_config_id to playback_history_events. Playback events
carry a provider item id (a Plex ratingKey, a Jellyfin/Emby item id) that is
only unique within one server, but identity resolution keyed those ids by
service *type* alone. With two servers of the same type - one Tracearr config
bound to two Plex servers, the whole point of multi-server support - the main
server's movie_versions rows shadowed the linked server's
supplemental_media_matches rows, so a play on the linked server was either
credited to whichever movie happened to share that ratingKey on the main
server, or dropped entirely. Either way "playback plays" read 0 for media
that had been watched.

Events observed on a non-main config had their resolved identity written from
the main server's ids, so that identity is cleared here and re-resolved on the
next playback refresh (forced by the bumped format versions in
backend/services/playback_history.py).

Revision ID: b8f4c2d6a9e3
Revises: a1c9e3f7b5d2
Create Date: 2026-08-28 00:00:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8f4c2d6a9e3"
down_revision: str | None = "a1c9e3f7b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MEDIA_SERVICES = ("PLEX", "JELLYFIN", "EMBY")


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def _indexes(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA index_list({table})"))}


def _extra_settings(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _backfill_tracearr_bindings(bind: sa.engine.Connection) -> None:
    """Attribute retained Tracearr events to the server their binding names.

    A Tracearr event key is "{tracearr_server_id}:{row id}", and the config's
    server_bindings map each Tracearr server to a Reclaimerr media server
    config, so every existing row can name its server without re-fetching.
    """

    media_types = {
        int(config_id): str(service_type)
        for config_id, service_type in bind.execute(
            sa.text(
                "SELECT id, service_type FROM service_configs "
                "WHERE service_type IN ('PLEX', 'JELLYFIN', 'EMBY')"
            )
        )
    }
    tracearr_configs = bind.execute(
        sa.text(
            "SELECT id, extra_settings FROM service_configs "
            "WHERE service_type = 'TRACEARR'"
        )
    ).all()

    for tracearr_config_id, raw_settings in tracearr_configs:
        bindings = _extra_settings(raw_settings).get("server_bindings")
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            server_id = str(binding.get("tracearr_server_id") or "").strip()
            server_type = str(binding.get("server_type") or "").strip().upper()
            try:
                media_config_id = int(binding.get("service_config_id"))
            except (TypeError, ValueError):
                continue
            if (
                not server_id
                or server_type not in _MEDIA_SERVICES
                or media_types.get(media_config_id) != server_type
            ):
                continue
            prefix = f"{server_id}:"
            bind.execute(
                sa.text(
                    """
                    UPDATE playback_history_events
                    SET observed_service_config_id = :media_config_id
                    WHERE observed_service_config_id IS NULL
                      AND source_service = 'TRACEARR'
                      AND source_service_config_id = :tracearr_config_id
                      AND substr(source_event_key, 1, :prefix_length) = :prefix
                    """
                ),
                {
                    "media_config_id": media_config_id,
                    "tracearr_config_id": tracearr_config_id,
                    "prefix_length": len(prefix),
                    "prefix": prefix,
                },
            )


def upgrade() -> None:
    bind = op.get_bind()

    if "observed_service_config_id" not in _cols("playback_history_events"):
        # Added with a raw ALTER rather than a batch rebuild: SQLite accepts a
        # REFERENCES clause on ADD COLUMN as long as the default is NULL, so the
        # FK lands without copying the whole event ledger.
        op.execute(
            sa.text(
                """
                ALTER TABLE playback_history_events
                ADD COLUMN observed_service_config_id INTEGER
                REFERENCES service_configs (id) ON DELETE SET NULL
                """
            )
        )

    if "ix_playback_history_events_observed_service_config_id" not in _indexes(
        "playback_history_events"
    ):
        op.create_index(
            "ix_playback_history_events_observed_service_config_id",
            "playback_history_events",
            ["observed_service_config_id"],
        )

    # Playback Reporting reads a media server's own history, so the provider
    # config and the observed server are the same row.
    op.execute(
        sa.text(
            """
            UPDATE playback_history_events
            SET observed_service_config_id = source_service_config_id
            WHERE observed_service_config_id IS NULL
              AND source_service IN ('JELLYFIN', 'EMBY')
            """
        )
    )

    # Tautulli cannot name which Plex it fronts, so it is attributed to the main
    # Plex server. Left null when main is not Plex: identity resolution falls
    # back to its pre-multi-server behaviour for unattributed rows.
    op.execute(
        sa.text(
            """
            UPDATE playback_history_events
            SET observed_service_config_id = (
                SELECT id FROM service_configs
                WHERE is_main = 1 AND service_type = 'PLEX' LIMIT 1
            )
            WHERE observed_service_config_id IS NULL
              AND source_service = 'TAUTULLI'
            """
        )
    )

    _backfill_tracearr_bindings(bind)

    # Anything observed on a linked server was resolved against the main
    # server's item ids and may point at the wrong media. Drop that identity so
    # the next refresh re-resolves it from the linked server's own supplemental
    # matches; aggregates are rebuilt from these rows, so nothing is lost.
    main_config_id = bind.execute(
        sa.text("SELECT id FROM service_configs WHERE is_main = 1 LIMIT 1")
    ).scalar()
    clear_identity = """
        UPDATE playback_history_events
        SET tmdb_id = NULL,
            season_number = NULL,
            episode_number = NULL,
            movie_id = NULL,
            series_id = NULL,
            season_id = NULL,
            episode_id = NULL
        WHERE observed_service_config_id IS NOT NULL
    """
    if main_config_id is None:
        bind.execute(sa.text(clear_identity))
    else:
        bind.execute(
            sa.text(
                f"{clear_identity} AND observed_service_config_id != :main_config_id"
            ),
            {"main_config_id": int(main_config_id)},
        )


def downgrade() -> None:
    if "ix_playback_history_events_observed_service_config_id" in _indexes(
        "playback_history_events"
    ):
        op.drop_index(
            "ix_playback_history_events_observed_service_config_id",
            table_name="playback_history_events",
        )
    with op.batch_alter_table("playback_history_events", schema=None) as batch_op:
        batch_op.drop_column("observed_service_config_id")
