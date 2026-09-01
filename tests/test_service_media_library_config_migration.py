"""The library table must learn which media server each row came from.

Library rows only ever come from the main media server, but nothing recorded
which one that was. Jellyfin and Emby derive a library's id from its path, so
two servers each holding a library at the same path report the same id -- and a
main-server switch then updated the old row in place, silently retargeting
every rule scoped to it.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "alembic"
    / "versions"
    / "a1c9e3f7b5d2_scope_service_media_libraries_to_config.py"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("_library_scope_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _BatchOp:
    def __init__(self, conn: sqlite3.Connection, table: str) -> None:
        self._conn = conn
        self._table = table

    def add_column(self, column: Any) -> None:
        self._conn.execute(
            f"ALTER TABLE {self._table} ADD COLUMN {column.name} INTEGER"
        )


class _Op:
    """Minimal stand-in for alembic's `op`, bound to a sqlite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_bind(self) -> Any:
        return self

    def execute(self, statement: Any) -> Any:
        return self._conn.execute(str(statement))

    @contextmanager
    def batch_alter_table(self, table: str, schema: Any = None) -> Any:
        yield _BatchOp(self._conn, table)

    def create_index(self, name: str, table: str, columns: list[str]) -> None:
        self._conn.execute(
            f"CREATE INDEX {name} ON {table} ({', '.join(columns)})"
        )


def _run(module: Any, conn: sqlite3.Connection, direction: str) -> None:
    module.op = _Op(conn)
    getattr(module, direction)()


def _connection(*, main_config_id: int | None = 3) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE service_configs "
        "(id INTEGER PRIMARY KEY, service_type TEXT, name TEXT, is_main BOOLEAN)"
    )
    conn.execute(
        """
        CREATE TABLE service_media_libraries (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            library_id VARCHAR(50) NOT NULL,
            library_name VARCHAR(255) NOT NULL,
            media_type VARCHAR(6) NOT NULL,
            selected BOOLEAN NOT NULL,
            added_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
            updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO service_configs (id, service_type, name, is_main) "
        "VALUES (2, 'PLEX', 'Plex Basement', 0)"
    )
    if main_config_id is not None:
        conn.execute(
            "INSERT INTO service_configs (id, service_type, name, is_main) "
            "VALUES (?, 'PLEX', 'Plex Living Room', 1)",
            (main_config_id,),
        )
    return conn


def _add_library(
    conn: sqlite3.Connection, library_id: str, name: str, media_type: str = "MOVIE"
) -> None:
    conn.execute(
        "INSERT INTO service_media_libraries "
        "(library_id, library_name, media_type, selected) VALUES (?, ?, ?, 1)",
        (library_id, name, media_type),
    )


def _rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return list(
        conn.execute(
            "SELECT library_id, library_name, service_config_id "
            "FROM service_media_libraries ORDER BY library_id"
        )
    )


def test_backfill_stamps_the_main_server_on_every_row():
    conn = _connection(main_config_id=3)
    _add_library(conn, "uuid-movies", "Movies")
    _add_library(conn, "uuid-shows", "Shows", media_type="SERIES")

    _run(_load_module(), conn, "upgrade")

    assert _rows(conn) == [
        ("uuid-movies", "Movies", 3),
        ("uuid-shows", "Shows", 3),
    ]


def test_rows_survive_with_no_main_server_configured():
    """An install that has not picked a main server yet has nothing to point at.

    Leaving the column NULL rather than dropping the rows keeps the library
    selections a user already made; the next sync adopts or removes them.
    """
    conn = _connection(main_config_id=None)
    _add_library(conn, "uuid-movies", "Movies")

    _run(_load_module(), conn, "upgrade")

    assert _rows(conn) == [("uuid-movies", "Movies", None)]


def test_upgrade_adds_the_config_scoped_unique_constraint():
    conn = _connection()
    _add_library(conn, "uuid-movies", "Movies")
    _run(_load_module(), conn, "upgrade")

    # The same library id under a different server is now representable - which
    # is the whole point, since Jellyfin gives two servers the same id for a
    # library at the same path.
    conn.execute(
        "INSERT INTO service_media_libraries "
        "(library_id, library_name, media_type, selected, service_config_id) "
        "VALUES ('uuid-movies', 'Movies', 'MOVIE', 1, 2)"
    )
    assert len(_rows(conn)) == 2

    try:
        conn.execute(
            "INSERT INTO service_media_libraries "
            "(library_id, library_name, media_type, selected, service_config_id) "
            "VALUES ('uuid-movies', 'Movies Again', 'MOVIE', 1, 3)"
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("expected a duplicate (config, library) to be rejected")


def test_upgrade_is_idempotent():
    conn = _connection()
    _add_library(conn, "uuid-movies", "Movies")
    module = _load_module()

    _run(module, conn, "upgrade")
    _run(module, conn, "upgrade")

    assert _rows(conn) == [("uuid-movies", "Movies", 3)]


def test_downgrade_round_trips():
    conn = _connection()
    _add_library(conn, "uuid-movies", "Movies")
    module = _load_module()

    _run(module, conn, "upgrade")
    _run(module, conn, "downgrade")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(service_media_libraries)")}
    assert "service_config_id" not in columns
    assert list(
        conn.execute(
            "SELECT library_id, library_name FROM service_media_libraries"
        )
    ) == [("uuid-movies", "Movies")]


def test_downgrade_collapses_rows_the_old_shape_cannot_hold():
    """The pre-change table has no room for a server, so two servers' copies of
    one library id must collapse rather than fail on insert."""
    conn = _connection()
    _add_library(conn, "shared-id", "Movies")
    module = _load_module()
    _run(module, conn, "upgrade")
    conn.execute(
        "INSERT INTO service_media_libraries "
        "(library_id, library_name, media_type, selected, service_config_id) "
        "VALUES ('shared-id', 'Movies', 'MOVIE', 1, 2)"
    )

    _run(module, conn, "downgrade")

    assert list(
        conn.execute("SELECT library_id FROM service_media_libraries")
    ) == [("shared-id",)]
