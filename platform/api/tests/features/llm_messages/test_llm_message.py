"""features/llm_messages/service.py — the llm_message batch insert and its
slice of the startup schema guard."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from hexgate_api.core.clickhouse import ZERO_RUN_ID, BatchItem, SchemaOutOfDate
from hexgate_api.features.llm_messages import service as llm_messages
from hexgate_api.features.llm_messages.service import (
    _LLM_MESSAGE_COLUMNS,
    insert_llm_messages_batch,
)
from hexgate_api.schemas import LlmMessageEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _message_event(**overrides) -> LlmMessageEvent:
    base = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc),
        "agent_name": "researcher",
        "model": "gpt-4o",
        "turn_key": "run_1:researcher",
        "message_seq": 0,
        "input_messages": json.dumps([{"role": "user", "parts": []}]),
        "output_messages": json.dumps([{"role": "assistant", "parts": []}]),
    }
    return LlmMessageEvent(**{**base, **overrides})


def _column(name: str) -> int:
    return _LLM_MESSAGE_COLUMNS.index(name)


# ---------------------------------------------------------------------------
# insert_llm_messages_batch()
# ---------------------------------------------------------------------------


def test_insert_llm_messages_batch_happy_path() -> None:
    """N per-item-resolved events become ONE insert call carrying N rows;
    project_id/agent_version_id stay per item (a consumer batch can span
    projects). Same contract as the other insert_*_batch functions."""
    clickhouse_client = MagicMock()
    items = [
        BatchItem(
            _message_event(message_seq=i),
            project_id=f"proj_{i}",
            agent_version_id=f"ver_{i}",
        )
        for i in range(3)
    ]

    insert_llm_messages_batch(clickhouse_client, items)

    clickhouse_client.insert.assert_called_once()
    args, kwargs = clickhouse_client.insert.call_args
    assert args[0] == "llm_message"
    rows = args[1]
    assert len(rows) == 3
    assert kwargs["column_names"] == _LLM_MESSAGE_COLUMNS
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
    assert [row[_column("message_seq")] for row in rows] == [0, 1, 2]


def test_when_the_batch_is_empty_then_clickhouse_is_not_called() -> None:
    clickhouse_client = MagicMock()

    insert_llm_messages_batch(clickhouse_client, [])

    clickhouse_client.insert.assert_not_called()


def test_when_a_row_is_built_then_every_column_is_filled_in_schema_order() -> None:
    """The row builder is the only place the wire model meets the table; a
    transposed or skipped column would land quietly in the wrong place."""
    run_id = uuid.uuid4()
    event = _message_event(
        session_id="sess_1",
        user_id="alice",
        system_instructions=json.dumps([{"type": "text", "content": "be terse"}]),
        run_id=run_id,
    )

    row = llm_messages._llm_message_row(event, project_id="p", agent_version_id="v")

    assert len(row) == len(_LLM_MESSAGE_COLUMNS)
    assert row[_column("event_id")] == event.event_id
    assert row[_column("occurred_at")] == event.occurred_at
    assert row[_column("project_id")] == "p"
    assert row[_column("agent_name")] == "researcher"
    assert row[_column("agent_version_id")] == "v"
    assert row[_column("session_id")] == "sess_1"
    assert row[_column("user_id")] == "alice"
    assert row[_column("model")] == "gpt-4o"
    assert row[_column("turn_key")] == "run_1:researcher"
    assert row[_column("input_messages")] == event.input_messages
    assert row[_column("output_messages")] == event.output_messages
    assert row[_column("system_instructions")] == event.system_instructions
    assert row[_column("run_id")] == run_id


def test_when_flags_are_set_then_stored_as_uint8_ints() -> None:
    """resynced/truncated are UInt8 columns: the row carries 0/1, never a
    Python bool the driver has to coerce."""
    flagged = llm_messages._llm_message_row(
        _message_event(resynced=True, truncated=True),
        project_id="p",
        agent_version_id="v",
    )
    plain = llm_messages._llm_message_row(
        _message_event(), project_id="p", agent_version_id="v"
    )

    for name in ("resynced", "truncated"):
        assert flagged[_column(name)] == 1
        assert plain[_column(name)] == 0
        assert type(flagged[_column(name)]) is int
        assert type(plain[_column(name)]) is int


def test_when_run_id_is_absent_then_the_zero_uuid_is_written() -> None:
    """Outside a run scope the SDK omits run_id; the column is UUID, not
    Nullable(UUID), so the row substitutes the zero UUID like llm_invocation."""
    row = llm_messages._llm_message_row(
        _message_event(), project_id="p", agent_version_id="v"
    )
    assert row[_column("run_id")] == ZERO_RUN_ID


def test_when_system_instructions_are_absent_then_the_row_carries_an_empty_string() -> (
    None
):
    """Only the first event of a turn_key carries instructions; the others
    write '' so the column default and the wire default agree."""
    row = llm_messages._llm_message_row(
        _message_event(), project_id="p", agent_version_id="v"
    )
    assert row[_column("system_instructions")] == ""


# ---------------------------------------------------------------------------
# verify_schema() — startup guard against deploying before the 0003 migration
# ---------------------------------------------------------------------------


def _describing(columns: list[str]) -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.column_names = ["name", "type"]
    result.result_rows = [[c, "String"] for c in columns]
    client.query.return_value = result
    return client


def test_verify_schema_happy_path() -> None:
    llm_messages.verify_schema(_describing(list(_LLM_MESSAGE_COLUMNS)))  # no raise


def test_when_a_written_column_is_missing_then_verify_schema_names_it() -> None:
    columns = [c for c in _LLM_MESSAGE_COLUMNS if c != "resynced"]

    with pytest.raises(SchemaOutOfDate) as exc:
        llm_messages.verify_schema(_describing(columns))

    assert exc.value.missing == {"llm_message": ["resynced"]}


def test_when_the_table_is_absent_then_verify_schema_reports_every_column() -> None:
    """The case this guard exists for: llm_message ships as a hand-applied
    migration, so a stage that never ran 0003 has no table at all."""
    client = MagicMock()
    client.query.side_effect = DatabaseError(
        "Received ClickHouse exception, code: 60, server response: "
        "Code: 60. DB::Exception: Table hexgate_audit.llm_message does not exist"
    )

    with pytest.raises(SchemaOutOfDate) as exc:
        llm_messages.verify_schema(client)

    assert exc.value.missing == {"llm_message": sorted(_LLM_MESSAGE_COLUMNS)}


def test_when_the_columns_match_schema_sql_then_nothing_is_missing() -> None:
    """Pins the column list to the table DDL: a column added to schema.sql
    but not written here would silently store its default, and one written
    here but absent from the DDL would fail every insert."""
    from pathlib import Path

    schema = Path(__file__).parents[4] / "clickhouse" / "init" / "schema.sql"
    ddl = schema.read_text()
    start = ddl.index("CREATE TABLE IF NOT EXISTS hexgate_audit.llm_message")
    body = ddl[start : ddl.index("ENGINE = ReplacingMergeTree", start)]
    declared = [
        line.split()[0]
        for line in body.splitlines()
        if line.startswith("    ") and not line.strip().startswith("--")
    ]
    # received_at is server-stamped and never written.
    assert [c for c in declared if c != "received_at"] == _LLM_MESSAGE_COLUMNS
