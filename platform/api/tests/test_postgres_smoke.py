"""Opt-in Postgres smoke test — the only coverage of the asyncpg path.

The rest of the suite builds its own in-memory SQLite engines and overrides
``get_session``, so it never exercises Postgres. This test points the real
engine at a live PG (via ``DATABASE_URL``), runs ``init_db`` against it, and
round-trips a row — proving the schema (FastAPI-Users tables, JSON +
LargeBinary columns, string-UUID PKs) builds and works on Postgres.

Skipped unless ``DATABASE_URL`` names a Postgres DSN — set by the
``platform-api-postgres`` CI job, or locally via ``make postgres-up``:

    DATABASE_URL=postgresql+asyncpg://hexgate:hexgate-dev-password@localhost:5433/hexgate \\
        uv run pytest tests/test_postgres_smoke.py -v
"""

from __future__ import annotations

import os

import pytest
from sqlmodel import select

pytestmark = pytest.mark.skipif(
    "postgres" not in os.environ.get("DATABASE_URL", ""),
    reason="set DATABASE_URL to a Postgres DSN to run (see `make postgres-up`)",
)


async def test_schema_builds_and_round_trips_on_postgres() -> None:
    # Imported lazily so the module collects (and skips) even if importing the
    # app eagerly built a Postgres engine that couldn't connect.
    import models
    from db import async_session_factory, engine, init_db

    # The engine must actually be talking asyncpg, not the SQLite fallback —
    # guards against a misconfigured DATABASE_URL silently passing the test.
    assert engine.url.drivername == "postgresql+asyncpg"

    await init_db()  # create_all against Postgres — the real schema port

    async with async_session_factory() as session:
        org = models.Organization(slug="pg-smoke", name="PG Smoke")
        session.add(org)
        await session.commit()

        fetched = await session.get(models.Organization, org.id)
        assert fetched is not None and fetched.slug == "pg-smoke"

        # exec() path too — the dialect-sensitive query layer, not just get().
        by_slug = (
            await session.exec(
                select(models.Organization).where(
                    models.Organization.slug == "pg-smoke"
                )
            )
        ).first()
        assert by_slug is not None and by_slug.id == org.id

        # Clean up so reruns against a persistent volume don't collide on the
        # unique slug.
        await session.delete(fetched)
        await session.commit()
