from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from backend.database import set_sqlite_pragma


def test_sqlite_connections_receive_locking_pragmas() -> None:
    tmp_root = Path("tests/.tmp")
    tmp_root.mkdir(parents=True, exist_ok=True)
    db_path = tmp_root / f"test_pragmas_{uuid4().hex}.db"
    connection = sqlite3.connect(db_path)
    try:
        set_sqlite_pragma(connection, None)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (30000,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (1,)
    finally:
        connection.close()
        if db_path.exists():
            db_path.unlink()
