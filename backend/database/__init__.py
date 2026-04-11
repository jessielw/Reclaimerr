from collections.abc import AsyncGenerator

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
)


@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_conn, _connection_record):
    """Set SQLite PRAGMA settings on each connection."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
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


async def init_db():
    """Initialize database tables and set WAL mode."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """
    Close database connections and dispose of the engine.
    Should be called during application shutdown.
    """
    await engine.dispose()
