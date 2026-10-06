"""features/agent_runs/service.py — the agent_run batch insert and its slice of
the startup schema guard."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError
from pydantic import ValidationError

from hexgate_api.core.clickhouse import BatchItem, SchemaOutOfDate
from hexgate_api.features.agent_runs import service as agent_runs
from hexgate_api.features.agent_runs.service import (
    _AGENT_RUN_COLUMNS,
    insert_agent_runs_batch,
)
from hexgate_api.schemas import AgentRunEvent

_SCHEMA_SQL = Path(__file__).parents[4] / "clickhouse" / "init" / "schema.sql"
_SERVER_STAMPED = "received_at"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_event(**overrides) -> AgentRunEvent:
    base = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc),
        "agent_name": "researcher",
        "run_id": str(uuid.uuid4()),
    }
    return AgentRunEvent(**{**base, **overrides})


def _column(name: str) -> int:
    return _AGENT_RUN_COLUMNS.index(name)


# ---------------------------------------------------------------------------
# AgentRunEvent
# ---------------------------------------------------------------------------


def test_when_run_id_is_absent_then_the_event_is_invalid() -> None:
    """Unlike the sibling events, there is no zero-UUID fallback: a run-start
    span outside a run is malformed and belongs in the DLQ."""
    with pytest.raises(ValidationError):
        AgentRunEvent(
            event_id=str(uuid.uuid4()),
            occurred_at=datetime.now(timezone.utc),
            agent_name="researcher",
        )


# ---------------------------------------------------------------------------
# insert_agent_runs_batch()
# ---------------------------------------------------------------------------


def test_insert_agent_runs_batch_happy_path() -> None:
    """N per-item-resolved events become ONE insert call carrying N rows, with
    project_id/agent_version_id kept per item."""
    clickhouse_client = MagicMock()
    items = [
        BatchItem(_run_event(), project_id=f"proj_{i}", agent_version_id=f"ver_{i}")
        for i in range(3)
    ]

    insert_agent_runs_batch(clickhouse_client, items)

    clickhouse_client.insert.assert_called_once()
    args, kwargs = clickhouse_client.insert.call_args
    assert args[0] == "agent_run"
    rows = args[1]
    assert len(rows) == 3
    assert kwargs["column_names"] == _AGENT_RUN_COLUMNS
    assert kwargs["settings"] == {"async_insert": 0}
    assert [row[_column("project_id")] for row in rows] == [
        "proj_0",
        "proj_1",
        "proj_2",
    ]
    assert [row[_column("agent_version_id")] for row in rows] == [
        "ver_0",
        "ver_1",
        "ver_2",
    ]


def test_when_the_batch_is_empty_then_clickhouse_is_not_called() -> None:
    clickhouse_client = MagicMock()

    insert_agent_runs_batch(clickhouse_client, [])

    clickhouse_client.insert.assert_not_called()


def test_when_a_row_is_built_then_every_column_is_filled_in_schema_order() -> None:
    event = _run_event(session_id="sess_1", user_id="alice")

    row = agent_runs._agent_run_row(event, project_id="p", agent_version_id="v")

    assert len(row) == len(_AGENT_RUN_COLUMNS)
    assert row[_column("event_id")] == event.event_id
    assert row[_column("occurred_at")] == event.occurred_at
    assert row[_column("project_id")] == "p"
    assert row[_column("agent_name")] == "researcher"
    assert row[_column("agent_version_id")] == "v"
    assert row[_column("session_id")] == "sess_1"
    assert row[_column("user_id")] == "alice"
    assert row[_column("run_id")] == event.run_id


# ---------------------------------------------------------------------------
# verify_schema() — startup guard against deploying before the 0004 migration
# ---------------------------------------------------------------------------


def _describing(columns: list[str]) -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.column_names = ["name", "type"]
    result.result_rows = [[c, "String"] for c in columns]
    client.query.return_value = result
    return client


def test_verify_schema_happy_path() -> None:
    agent_runs.verify_schema(_describing(list(_AGENT_RUN_COLUMNS)))  # no raise


def test_when_a_written_column_is_missing_then_verify_schema_names_it() -> None:
    columns = [c for c in _AGENT_RUN_COLUMNS if c != "run_id"]

    with pytest.raises(SchemaOutOfDate) as exc:
        agent_runs.verify_schema(_describing(columns))

    assert exc.value.missing == {"agent_run": ["run_id"]}


def test_when_the_table_is_absent_then_verify_schema_reports_every_column() -> None:
    """The case this guard exists for: a stage that never ran 0004."""
    client = MagicMock()
    client.query.side_effect = DatabaseError(
        "Received ClickHouse exception, code: 60, server response: "
        "Code: 60. DB::Exception: Table hexgate_audit.agent_run does not exist"
    )

    with pytest.raises(SchemaOutOfDate) as exc:
        agent_runs.verify_schema(client)

    assert exc.value.missing == {"agent_run": sorted(_AGENT_RUN_COLUMNS)}


def test_when_the_columns_match_schema_sql_then_nothing_is_missing() -> None:
    """Pins the column list to the table DDL, so writer and table can't drift."""
    ddl = _SCHEMA_SQL.read_text()
    start = ddl.index("CREATE TABLE IF NOT EXISTS hexgate_audit.agent_run")
    body = ddl[start : ddl.index("ENGINE = ReplacingMergeTree", start)]
    declared = [
        line.split()[0]
        for line in body.splitlines()
        if line.startswith("    ") and not line.strip().startswith("--")
    ]
    assert [c for c in declared if c != _SERVER_STAMPED] == _AGENT_RUN_COLUMNS


def test_migration_0004_creates_the_same_table_as_schema_sql() -> None:
    """The init script only runs on an empty volume; a hand-migrated one must
    end up with the identical table."""
    statement_start = "CREATE TABLE IF NOT EXISTS hexgate_audit.agent_run"
    migration = (
        _SCHEMA_SQL.parents[1] / "migrations" / "0004_add_agent_run.sql"
    ).read_text()
    ddl = _SCHEMA_SQL.read_text()

    def _statement(text: str) -> str:
        start = text.index(statement_start)
        return text[start : text.index(";", start) + 1]

    assert _statement(migration) == _statement(ddl)
