"""Async SQLAlchemy engine + session factory.

``DATABASE_URL`` selects Postgres (asyncpg) in deployment; unset falls back
to a local SQLite file so dev and tests stay zero-setup.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

# The engine is built at import, before main's load_dotenv(), so load .env
# here too. Non-overriding, so real env vars still win.
load_dotenv()

DB_PATH = Path(__file__).parent / "hexgate.db"
_DEFAULT_URL = f"sqlite+aiosqlite:///{DB_PATH}"


def _database_url() -> str:
    """Resolve the async DB URL: ``DATABASE_URL`` or the SQLite fallback.

    Bare ``postgres(ql)://`` URLs are rewritten to the ``asyncpg`` driver
    the async engine requires.
    """
    url = os.environ.get("DATABASE_URL", "").strip() or _DEFAULT_URL
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


# pool_pre_ping tolerates connections dropped by a managed Postgres; harmless
# on SQLite.
engine = create_async_engine(_database_url(), echo=False, pool_pre_ping=True)

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
    migration story is still ``rm hexgate.db && restart``; Alembic
    lands when there's production data to preserve.
    """
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session():
    """FastAPI dependency that yields a per-request async session.

    Lives in ``db.py`` (not ``main.py``) so the auth layer can depend
    on it without inducing a cycle through ``main``. Every route
    handler in ``main.py`` continues to import it from here.
    """
    async with async_session_factory() as session:
        yield session
