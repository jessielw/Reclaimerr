from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

from backend.core.settings import settings


class Base(MappedAsDataclass, DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


engine = create_async_engine(
    f"sqlite+aiosqlite:///{settings.db_path}",
    echo=False,
    future=True,
    connect_args={"timeout": 30.0},
)


# PRAGMA foreign_keys is connection state, not database state, so it has to be
# set on every connection the pool hands out. Setting it per request instead
# left enforcement down to whoever opened the connection first: a background
# task that borrowed a connection a request had already used ran with foreign
# keys on, the same task on a fresh connection ran with them off, so ON DELETE
# CASCADE / SET NULL fired or silently did not depending on pool luck.
#
# Migrations are the one place it has to stay off: a SQLite batch rebuild drops
# and renames tables other tables reference, which enforcement refuses. It also
# cannot be turned off per statement, because PRAGMA foreign_keys is a no-op
# inside a transaction and migrations run in one - hence the flag here.
_migrating = False


@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_conn: Any, _connection_record: Any) -> None:
    """Set SQLite PRAGMA settings on each connection."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA journal_mode=WAL")
    if not _migrating:
        cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


async_db = async_sessionmaker[AsyncSession](
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for getting async database sessions.

    Usage in FastAPI:
        @app.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with async_db() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Run pending Alembic migrations and configure WAL mode."""
    # derive the alembic scripts path from __file__ so it resolves correctly
    # in all three deployment modes (docker, source, pyinstaller bundle) without
    # depending on CWD or alembic.ini being present at runtime.
    # alembic.ini is still used by the CLI (alembic revision --autogenerate).
    cfg = AlembicConfig()
    cfg.set_main_option(
        "script_location", str(Path(__file__).resolve().parent.parent / "alembic")
    )
    global _migrating
    _migrating = True
    try:
        async with engine.begin() as conn:
            # inject the open connection into config.attributes so env.py reuses
            # it directly instead of opening a second conflicting SQLite connection.
            await conn.run_sync(_run_alembic_upgrade, cfg)
    finally:
        _migrating = False

    # The migration connection goes back to the pool with foreign keys off.
    # Drop it so the next checkout reconnects through the hook above and every
    # connection the app runs on enforces them. Nothing else has touched the
    # pool yet - init_db is the first thing startup does with the database.
    await engine.dispose()


def _run_alembic_upgrade(sync_conn: Any, cfg: AlembicConfig) -> None:
    """Synchronous bridge called by run_sync to execute Alembic upgrades."""
    cfg.attributes["connection"] = sync_conn
    alembic_command.upgrade(cfg, "head")


async def close_db() -> None:
    """
    Close database connections and dispose of the engine.
    Should be called during application shutdown.
    """
    await engine.dispose()
