"""Opt-in Postgres smoke test — the only coverage of the asyncpg path.

The rest of the suite runs on SQLite, so this is the only test that points
the real engine at a live PG and round-trips a row. Skipped unless
``DATABASE_URL`` names a Postgres DSN (set by the ``platform-api-postgres``
CI job, or locally via ``make postgres-up``).
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
    # Lazy import so the module still collects (and skips) if a bad
    # DATABASE_URL made the app's engine fail to build.
    from hexgate_api import models
    from hexgate_api.core.db import async_session_factory, engine, init_db

    # Guard against a misconfigured DATABASE_URL silently using SQLite.
    assert engine.url.drivername == "postgresql+asyncpg"

    await init_db()  # create_all against Postgres

    async with async_session_factory() as session:
        # Self-heal a row leaked by a prior run that failed before cleanup.
        await _delete_smoke_org(session)

        org = models.Organization(slug="pg-smoke", name="PG Smoke")
        session.add(org)
        await session.commit()

        try:
            fetched = await session.get(models.Organization, org.id)
            assert fetched is not None and fetched.slug == "pg-smoke"

            # exec() path too — the dialect-sensitive query layer.
            by_slug = (
                await session.exec(
                    select(models.Organization).where(
                        models.Organization.slug == "pg-smoke"
                    )
                )
            ).first()
            assert by_slug is not None and by_slug.id == org.id
        finally:
            # Clean up even on assertion failure, so reruns against a
            # persistent volume don't collide on the unique slug.
            await _delete_smoke_org(session)


async def _delete_smoke_org(session) -> None:
    """Delete the pg-smoke org if present."""
    from hexgate_api import models

    existing = (
        await session.exec(
            select(models.Organization).where(models.Organization.slug == "pg-smoke")
        )
    ).first()
    if existing is not None:
        await session.delete(existing)
        await session.commit()


async def test_hand_applied_migrations_match_the_live_schema() -> None:
    """Every column the migrations add really exists on this Postgres.

    The rest of the suite builds its schema with ``create_all`` on SQLite, so
    nothing else can catch the two failure modes that only bite a deployed
    database:

      * the migration was never applied (``create_all`` adds missing *tables*,
        never missing columns, so startup is silent and requests 500);
      * the SQL has a typo — ``ADD COLUMN IF NOT EXISTS created_by_user_i`` is
        valid, adds a column nothing reads, and leaves the real one missing.

    Read-only by design: it inspects ``information_schema`` rather than
    dropping and re-adding columns, so it is safe to run against a dev volume
    that holds real rows.
    """
    import re
    from pathlib import Path

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from hexgate_api.core.db import _database_url

    # parents: [0] core, [1] tests, [2] api, [3] platform
    migrations = sorted(
        (Path(__file__).resolve().parents[3] / "postgres" / "migrations").glob("*.sql")
    )
    assert migrations, "no migration files found"
    sql = "\n".join(f.read_text() for f in migrations)

    expected = {
        (table.lower(), column.lower())
        for table, column in re.findall(
            r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)",
            sql,
            re.IGNORECASE,
        )
    }
    expected_indexes = {
        name.lower()
        for name, _table in re.findall(
            r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)",
            sql,
            re.IGNORECASE,
        )
    }
    assert expected, "the migration regex matched nothing — did the SQL style change?"

    # A dedicated engine, disposed here: the module-level one pools connections
    # bound to whichever event loop first used it, and pytest-asyncio gives each
    # test its own loop.
    probe = create_async_engine(_database_url())
    try:
        async with probe.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
            )
            live = {(t.lower(), c.lower()) for t, c in rows.all()}
            index_rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
            live_indexes = {name.lower() for (name,) in index_rows.all()}
    finally:
        await probe.dispose()

    missing = sorted(expected - live)
    assert not missing, (
        "columns the migrations add are absent from this database: "
        + ", ".join(f"{t}.{c}" for t, c in missing)
        + " — run `make postgres-init` (or `make platform-migrate STAGE=…`)"
    )

    missing_indexes = sorted(expected_indexes - live_indexes)
    assert not missing_indexes, (
        f"indexes the migrations create are absent: {missing_indexes}"
    )
