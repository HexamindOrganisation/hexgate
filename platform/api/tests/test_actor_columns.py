"""Tripwires for the control-plane actor trail (issue #160).

These assert no behaviour. They exist because both mistakes are silent: a new
table with no actor columns goes unnoticed until an auditor asks who wrote a
row, and a model column with no ``ALTER TABLE`` passes on SQLite while 500ing
every deployed request.
"""

from __future__ import annotations

import re
from pathlib import Path

import hexgate_api.models  # noqa: F401 — registers every table on the metadata
from sqlmodel import SQLModel

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[3] / "platform" / "postgres" / "migrations"
)

CREATION_ACTOR_COLUMN = "created_by_user_id"

# Tables with no creation actor, and why. A fourth entry needs an argument in
# review, not a silent omission.
EXEMPT_TABLES: dict[str, str] = {
    "user": "self-created at register; the table is owned by FastAPI Users",
    "oauth_account": "created by FastAPI Users when an OAuth login lands",
    "tool": (
        "child of agent_version, only written as part of a version snapshot and "
        "never mutated alone, so it inherits that row's trail"
    ),
}

# Tables that record their creator under a domain-specific name.
CREATION_ACTOR_ALIASES: dict[str, str] = {
    "invitation": "invited_by_user_id",
}

# (table, column) pairs added to a table that ALREADY EXISTED, so only a
# hand-written migration reaches a deployed database. A brand-new table needs
# no entry -- ``create_all`` creates it whole.
COLUMNS_NEEDING_A_MIGRATION: set[tuple[str, str]] = {
    ("devtoken", "revoked_at"),
    ("devtoken", "revoked_by_user_id"),
    ("devtoken", "created_by_user_id"),
    ("devtoken", "owner_user_id"),
    ("organization", "created_by_user_id"),
    ("organization", "updated_at"),
    ("organization", "updated_by_user_id"),
    ("organization_member", "created_by_user_id"),
    ("organization_member", "updated_at"),
    ("organization_member", "updated_by_user_id"),
    ("invitation", "revoked_by_user_id"),
    ("project", "created_by_user_id"),
    ("project", "updated_at"),
    ("project", "updated_by_user_id"),
    ("agent", "created_by_user_id"),
    ("agent", "updated_by_user_id"),
    ("agent_version", "created_by_user_id"),
    ("policy_module", "created_by_user_id"),
    ("policy_module", "updated_by_user_id"),
    ("role_binding", "created_at"),
    ("role_binding", "created_by_user_id"),
    # policy_file shipped in #179 without an actor trail, so the table already
    # existed on deployed stacks by the time #160 reached it -- migration 0003.
    ("policy_file", "created_by_user_id"),
    ("policy_file", "updated_by_user_id"),
}

_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)",
    re.IGNORECASE,
)
_CREATE_INDEX = re.compile(
    r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)", re.IGNORECASE
)


def _migration_sql() -> str:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert files, f"no migrations found under {MIGRATIONS_DIR}"
    return "\n".join(f.read_text() for f in files)


def test_every_table_carries_a_creation_actor_or_is_explicitly_exempt() -> None:
    """A new table without an actor trail must argue for it in EXEMPT_TABLES.

    Eleven of thirteen tables were missing one, unnoticed, before #160.
    """
    missing = []
    for name, table in sorted(SQLModel.metadata.tables.items()):
        if name in EXEMPT_TABLES:
            continue
        column = CREATION_ACTOR_ALIASES.get(name, CREATION_ACTOR_COLUMN)
        if column not in table.c:
            missing.append(f"{name} (expected {column})")
    assert not missing, (
        "tables with no creation actor: "
        + ", ".join(missing)
        + " — add the column, or add the table to EXEMPT_TABLES with a reason"
    )


def test_update_actor_lands_only_on_tables_mutated_in_place() -> None:
    """``updated_by_user_id`` is deliberately NOT on every table (decision D3).

    It only means something where a row is edited in place. ``role_binding`` is
    replaced wholesale, ``agent_version`` / ``tool`` are immutable, and a
    ``devtoken``'s one real mutation already carries ``revoked_by_user_id``.
    Growing this set is a design decision — make it an explicit one.
    """
    expected = {
        "organization",
        "organization_member",
        "project",
        "agent",
        "policy_module",
        "policy_file",
    }
    actual = {
        name
        for name, table in SQLModel.metadata.tables.items()
        if "updated_by_user_id" in table.c
    }
    assert actual == expected


def test_every_actor_column_on_a_pre_existing_table_has_a_migration() -> None:
    """``create_all`` covers fresh databases; deployed ones need the SQL file."""
    sql = _migration_sql()
    altered = {(t.lower(), c.lower()) for t, c in _ADD_COLUMN.findall(sql)}
    missing = sorted(COLUMNS_NEEDING_A_MIGRATION - altered)
    assert not missing, (
        "declared on the model but never added by a migration: "
        + ", ".join(f"{t}.{c}" for t, c in missing)
    )


def test_every_migrated_column_and_index_exists_on_the_model() -> None:
    """The other direction, and the one a typo actually trips.

    ``ADD COLUMN IF NOT EXISTS created_by_user_i`` is valid SQL: psql adds a
    column nothing reads and leaves the real one missing. Same for an index
    name that misses SQLAlchemy's ``ix_<table>_<column>`` default.
    """
    sql = _migration_sql()
    tables = SQLModel.metadata.tables

    for table_name, column in _ADD_COLUMN.findall(sql):
        assert table_name in tables, f"migration alters unknown table {table_name!r}"
        assert column in tables[table_name].c, (
            f"migration adds {table_name}.{column}, which no model declares — "
            "typo, or a column removed from the model but left in the SQL"
        )

    for index_name, table_name in _CREATE_INDEX.findall(sql):
        assert table_name in tables, f"migration indexes unknown table {table_name!r}"
        declared = {index.name for index in tables[table_name].indexes}
        assert index_name in declared, (
            f"migration creates index {index_name!r} on {table_name}, but the "
            f"model declares {sorted(declared)} — a fresh create_all database "
            "and a migrated one would disagree"
        )
