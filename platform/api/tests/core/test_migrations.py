"""Execute the migration files — the only coverage that they actually run.

``test_postgres_smoke.test_hand_applied_migrations_match_the_live_schema``
regexes ``ADD COLUMN`` out of the SQL and checks those names against a
database ``create_all`` just built, so the columns are present because the
*models* declare them. A migration that cannot execute at all passes it. That
is how ``relation "policy_file" does not exist`` reached a prod deploy.

These tests run the files. Each builds its own scratch database, so they
cannot order-couple through shared state.

Postgres: skipped unless ``DATABASE_URL`` names one (``make postgres-up``).
ClickHouse: skipped unless ``HEXGATE_CLICKHOUSE_HOST`` is set
(``make clickhouse-up`` sets nothing — export it, or let CI's service do it).
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest

# parents: [0] core, [1] tests, [2] api, [3] platform
_PLATFORM = Path(__file__).resolve().parents[3]
POSTGRES_MIGRATIONS = _PLATFORM / "postgres" / "migrations"
CLICKHOUSE_MIGRATIONS = _PLATFORM / "clickhouse" / "migrations"
CLICKHOUSE_INIT_SCHEMA = _PLATFORM / "clickhouse" / "init" / "schema.sql"

# Same patterns test_postgres_smoke.py uses, so the two tests cannot disagree
# about what the migrations claim to add.
ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE
)
CREATE_INDEX_RE = re.compile(
    r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)", re.IGNORECASE
)

CLICKHOUSE_DATABASE = "hexgate_audit"
AUDIT_TABLES = ("policy_decision", "llm_invocation", "llm_message", "ban_enforcement")

pg_only = pytest.mark.skipif(
    "postgres" not in os.environ.get("DATABASE_URL", ""),
    reason="set DATABASE_URL to a Postgres DSN to run (see `make postgres-up`)",
)
clickhouse_only = pytest.mark.skipif(
    not os.environ.get("HEXGATE_CLICKHOUSE_HOST"),
    reason="set HEXGATE_CLICKHOUSE_HOST to run (see `make clickhouse-up`)",
)


def migration_files(directory: Path) -> list[Path]:
    """Every ``.sql`` in filename order — the order the runner applies them."""
    files = sorted(directory.glob("*.sql"))
    assert files, f"no migration files in {directory}"
    return files


# --- Postgres ----------------------------------------------------------------


@dataclass(frozen=True)
class PostgresSchema:
    """What a migrated database must share with a fresh one.

    Columns and indexes only, deliberately: the actor columns' foreign keys are
    a *known and permanent* divergence. ``create_all`` emits
    ``ON DELETE SET NULL`` (models.actor_fk_column) while the migrations add
    bare ``varchar`` with no ``REFERENCES`` to avoid locking a live table —
    0002's header records that as intended. Comparing constraints would fail on
    a difference we chose.
    """

    columns: frozenset[tuple[str, str, str, str]]
    indexes: frozenset[str]


def _asyncpg_dsn(database: str | None = None) -> str:
    """``DATABASE_URL`` in asyncpg's dialect, optionally pointed elsewhere."""
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    if database is None:
        return dsn
    return dsn.rsplit("/", 1)[0] + f"/{database}"


@asynccontextmanager
async def scratch_database(name: str) -> AsyncIterator[str]:
    """Create an empty database, yield its DSN, drop it on the way out."""
    admin = await asyncpg.connect(_asyncpg_dsn())
    try:
        # A prior crashed run may have left it behind.
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    try:
        yield _asyncpg_dsn(name)
    finally:
        admin = await asyncpg.connect(_asyncpg_dsn())
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


async def replay_postgres(dsn: str, files: Sequence[Path]) -> None:
    """Apply each file as ONE script, in order.

    Not ``exec_driver_sql``: SQLAlchemy routes it through asyncpg's prepared
    statement path, which rejects a multi-command string with
    ``cannot insert multiple commands into a prepared statement``. asyncpg's own
    ``execute`` uses the simple query protocol and takes a whole file. Splitting
    on ``;`` is not an option either — every migration is ``DO $$ … END $$;``.
    """
    conn = await asyncpg.connect(dsn)
    try:
        for path in files:
            await conn.execute(path.read_text())
    finally:
        await conn.close()


async def snapshot_postgres(dsn: str) -> PostgresSchema:
    conn = await asyncpg.connect(dsn)
    try:
        columns = await conn.fetch(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema = 'public'"
        )
        indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        )
    finally:
        await conn.close()
    return PostgresSchema(
        columns=frozenset(
            (r["table_name"], r["column_name"], r["data_type"], r["is_nullable"])
            for r in columns
        ),
        indexes=frozenset(r["indexname"] for r in indexes),
    )


async def create_all_into(dsn: str) -> None:
    """Build the schema the models declare — what a brand-new stage gets."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel

    # Load-bearing: registers every table on SQLModel.metadata. Without it
    # create_all silently builds NOTHING (same reason api-init imports it).
    from hexgate_api import models  # noqa: F401

    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    finally:
        await engine.dispose()


def declared_changes(files: Sequence[Path]) -> tuple[set[tuple[str, str]], set[str]]:
    """The (table, column) pairs and index names the migrations claim to add."""
    sql = "\n".join(path.read_text() for path in files)
    columns = {(t.lower(), c.lower()) for t, c in ADD_COLUMN_RE.findall(sql)}
    indexes = {name.lower() for name, _table in CREATE_INDEX_RE.findall(sql)}
    assert columns, "the migration regex matched nothing — did the SQL style change?"
    return columns, indexes


async def strip_migrated_objects(dsn: str, files: Sequence[Path]) -> None:
    """Undo what the migrations add, simulating a pre-release schema."""
    columns, indexes = declared_changes(files)
    conn = await asyncpg.connect(dsn)
    try:
        for index in sorted(indexes):
            await conn.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table, column in sorted(columns):
            await conn.execute(
                f'ALTER TABLE IF EXISTS "{table}" DROP COLUMN IF EXISTS "{column}"'
            )
    finally:
        await conn.close()


@pg_only
async def test_every_migration_replays_against_an_empty_database() -> None:
    """The prod failure, pinned.

    0003 ran against a stack predating #179 and died on a missing
    ``policy_file``. This is the executable form of the guard convention: the
    directory is safe to replay against any stage, however far behind —
    including one with no tables at all.
    """
    async with scratch_database("hexgate_migrations_empty") as dsn:
        await replay_postgres(dsn, migration_files(POSTGRES_MIGRATIONS))


@pg_only
async def test_a_migrated_database_matches_a_fresh_one() -> None:
    """The invariant that actually matters.

    Nullability is not incidental detail here — it is the field that exposed
    four columns where migrated stages disagreed with every fresh database.
    """
    files = migration_files(POSTGRES_MIGRATIONS)

    async with scratch_database("hexgate_migrations_fresh") as fresh_dsn:
        await create_all_into(fresh_dsn)
        fresh = await snapshot_postgres(fresh_dsn)

    async with scratch_database("hexgate_migrations_upgraded") as upgraded_dsn:
        await create_all_into(upgraded_dsn)
        await strip_migrated_objects(upgraded_dsn, files)
        await replay_postgres(upgraded_dsn, files)
        upgraded = await snapshot_postgres(upgraded_dsn)

    assert upgraded.columns == fresh.columns, (
        "a migrated database does not match a fresh one: "
        f"only after migrating={sorted(upgraded.columns - fresh.columns)} "
        f"only in create_all={sorted(fresh.columns - upgraded.columns)}"
    )
    assert upgraded.indexes == fresh.indexes, (
        f"index drift: only migrated={sorted(upgraded.indexes - fresh.indexes)} "
        f"only fresh={sorted(fresh.indexes - upgraded.indexes)}"
    )


@pg_only
async def test_replaying_the_migrations_twice_changes_nothing() -> None:
    """Idempotency — asserted in every migration header, verified nowhere.

    The whole operating model rests on it: ``platform-migrate`` replays the
    entire directory on every upgrade.
    """
    files = migration_files(POSTGRES_MIGRATIONS)
    async with scratch_database("hexgate_migrations_twice") as dsn:
        await create_all_into(dsn)
        await strip_migrated_objects(dsn, files)

        await replay_postgres(dsn, files)
        after_first = await snapshot_postgres(dsn)

        await replay_postgres(dsn, files)
        after_second = await snapshot_postgres(dsn)

    assert after_first == after_second, "a second replay changed the schema"


# --- ClickHouse ---------------------------------------------------------------
#
# The store whose migration took both stages down. It has no create_all twin —
# init/schema.sql is the greenfield path — so the equivalence test compares a
# volume built from schema.sql alone against one built from schema.sql plus the
# migrations. That is the enforceable form of 0003's "keep this byte-identical
# to init/schema.sql" header, which nothing checked.


@contextmanager
def clickhouse_client(database: str | None = None) -> Iterator[object]:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=os.environ["HEXGATE_CLICKHOUSE_HOST"],
        port=int(os.environ.get("HEXGATE_CLICKHOUSE_PORT", "8124")),
        username=os.environ.get("HEXGATE_CLICKHOUSE_USER", "hexgate"),
        password=os.environ.get("HEXGATE_CLICKHOUSE_PASSWORD", "hexgate-dev-password"),
        database=database or "default",
        autogenerate_session_id=False,
    )
    try:
        yield client
    finally:
        client.close()


def split_statements(sql: str) -> list[str]:
    """Split SQL on the semicolons that actually end a statement.

    ``--multiquery`` does this for the deploy runner, but that is a
    clickhouse-client feature: the HTTP interface takes one statement per
    request, so the test has to split. Neither naive approach survives these
    files — ``--`` comments carry prose full of semicolons, and
    ``COMMENT 'SDK-truncated JSON snapshot; may be lossy'`` puts one inside a
    string literal. So track both, and skip a semicolon in either.
    """
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    in_comment = False
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if in_comment:
            if char == "\n":
                in_comment = False
                current.append(char)
        elif in_string:
            current.append(char)
            if char == "\\" and following:
                current.append(following)
                index += 1
            elif char == "'":
                in_string = False
        elif char == "-" and following == "-":
            in_comment = True
            index += 1
        elif char == "'":
            in_string = True
            current.append(char)
        elif char == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    statements.append("".join(current))
    return [statement.strip() for statement in statements if statement.strip()]


def run_clickhouse_script(client, sql: str, database: str) -> None:
    """Apply one file, retargeted onto a scratch database.

    Every statement is qualified ``hexgate_audit.<table>`` — the deploy runner
    relies on that — so pointing a file elsewhere rewrites the prefix rather
    than switching the session's default database.
    """
    retargeted = sql.replace(f"{CLICKHOUSE_DATABASE}.", f"{database}.")
    for statement in split_statements(retargeted):
        client.command(statement)


def test_split_statements_ignores_semicolons_in_comments_and_strings() -> None:
    """No database needed — the splitter is the fragile part of the CH tests."""
    sql = """
    -- a comment; with a semicolon and a period.
    CREATE TABLE t (a String COMMENT 'has; a semicolon') ENGINE = Memory;
    ALTER TABLE t ADD COLUMN b String;
    """
    statements = split_statements(sql)
    assert len(statements) == 2, statements
    assert statements[0].startswith("CREATE TABLE t")
    assert "has; a semicolon" in statements[0]
    assert statements[1] == "ALTER TABLE t ADD COLUMN b String"


def clickhouse_columns(client, database: str) -> frozenset[tuple[str, str, str]]:
    rows = client.query(
        "SELECT table, name, type FROM system.columns "
        f"WHERE database = '{database}' AND table IN {AUDIT_TABLES}"
    ).result_rows
    return frozenset((t, n, ty) for t, n, ty in rows)


@contextmanager
def scratch_clickhouse_database(name: str) -> Iterator[str]:
    with clickhouse_client() as client:
        client.command(f"DROP DATABASE IF EXISTS {name}")
        client.command(f"CREATE DATABASE {name}")
    try:
        yield name
    finally:
        with clickhouse_client() as client:
            client.command(f"DROP DATABASE IF EXISTS {name}")


@clickhouse_only
def test_clickhouse_migrations_replay_onto_a_fresh_schema() -> None:
    with scratch_clickhouse_database("hexgate_ch_replay") as database:
        with clickhouse_client() as client:
            run_clickhouse_script(client, CLICKHOUSE_INIT_SCHEMA.read_text(), database)
            for path in migration_files(CLICKHOUSE_MIGRATIONS):
                run_clickhouse_script(client, path.read_text(), database)


CH_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+\w+\.(\w+)((?:\s*,?\s*ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+\w+[^;]*?)+);",
    re.IGNORECASE | re.DOTALL,
)
CH_COLUMN_NAME_RE = re.compile(
    r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE
)
CH_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+\w+\.(\w+)", re.IGNORECASE
)


def strip_clickhouse_objects(client, database: str, files: Sequence[Path]) -> None:
    """Undo what the migrations add, simulating a volume that predates them.

    Without this the equivalence test proves nothing: ``schema.sql`` already
    builds every table, so ``CREATE TABLE IF NOT EXISTS`` in 0003 is a no-op and
    its column definitions are never executed. Dropping first is what forces the
    migration's own DDL to run — and what makes a drift from ``schema.sql``
    visible.
    """
    sql = "\n".join(path.read_text() for path in files)
    for table in CH_CREATE_TABLE_RE.findall(sql):
        client.command(f"DROP TABLE IF EXISTS {database}.{table}")
    for table, clauses in CH_ADD_COLUMN_RE.findall(sql):
        for column in CH_COLUMN_NAME_RE.findall(clauses):
            client.command(
                f"ALTER TABLE {database}.{table} DROP COLUMN IF EXISTS {column}"
            )


@clickhouse_only
def test_a_migrated_clickhouse_matches_a_fresh_one() -> None:
    """0003's "keep this byte-identical to init/schema.sql" header, enforced.

    Nothing checked it before — ``test_llm_message.py`` pins the column *list*
    to ``schema.sql``, not the migration to it.
    """
    migrations = migration_files(CLICKHOUSE_MIGRATIONS)

    with scratch_clickhouse_database("hexgate_ch_fresh") as fresh:
        with clickhouse_client() as client:
            run_clickhouse_script(client, CLICKHOUSE_INIT_SCHEMA.read_text(), fresh)
            fresh_columns = clickhouse_columns(client, fresh)

    with scratch_clickhouse_database("hexgate_ch_migrated") as migrated:
        with clickhouse_client() as client:
            run_clickhouse_script(client, CLICKHOUSE_INIT_SCHEMA.read_text(), migrated)
            strip_clickhouse_objects(client, migrated, migrations)
            for path in migrations:
                run_clickhouse_script(client, path.read_text(), migrated)
            migrated_columns = clickhouse_columns(client, migrated)

    assert migrated_columns == fresh_columns, (
        "the ClickHouse migrations and init/schema.sql disagree: "
        f"only migrated={sorted(migrated_columns - fresh_columns)} "
        f"only fresh={sorted(fresh_columns - migrated_columns)}"
    )


@clickhouse_only
def test_replaying_the_clickhouse_migrations_twice_changes_nothing() -> None:
    migrations = migration_files(CLICKHOUSE_MIGRATIONS)
    with scratch_clickhouse_database("hexgate_ch_twice") as database:
        with clickhouse_client() as client:
            run_clickhouse_script(client, CLICKHOUSE_INIT_SCHEMA.read_text(), database)
            for path in migrations:
                run_clickhouse_script(client, path.read_text(), database)
            after_first = clickhouse_columns(client, database)

            for path in migrations:
                run_clickhouse_script(client, path.read_text(), database)
            after_second = clickhouse_columns(client, database)

    assert after_first == after_second, "a second replay changed the schema"
