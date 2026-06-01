"""Async SQLAlchemy engine + session factory.

The platform runs on async I/O end-to-end (M3 Phase 3 prerequisite) —
every route handler is ``async def``, every dependency yields an
``AsyncSession``, every ``session.exec`` / ``session.get`` /
``session.commit`` / ``session.refresh`` is awaited.

SQLite is single-writer regardless of driver; ``aiosqlite`` just stops
disk I/O from blocking the event loop. Postgres will swap this for
``asyncpg`` later via env-switched ``DATABASE_URL``.
"""

from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

DB_PATH = Path(__file__).parent / "fortify.db"

# Async engine. ``aiosqlite`` requires the ``sqlite+aiosqlite://`` URL prefix
# — the plain ``sqlite://`` driver is sync-only. ``check_same_thread`` is
# unnecessary on async: each task gets its own connection from the pool.
engine = create_async_engine(
    f"sqlite+aiosqlite:///{DB_PATH}",
    echo=False,
)

# Session factory — used by ``get_session()`` and one-off scripts (seeds,
# tests). ``expire_on_commit=False`` keeps ORM objects usable after a
# commit without a refetch — the sync default expires every attribute
# and forces a re-load, which is doubly expensive on async.
async_session_factory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    """Create all tables defined on ``SQLModel.metadata``.

    Called once at app startup via the lifespan handler. Idempotent —
    ``create_all`` skips tables that already exist. Prototype-phase
    migration story is still ``rm fortify.db && restart``; Alembic
    lands when there's production data to preserve.
    """
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
