"""ClickHouse write path for agent_run rows (scope ``hexgate.runs``).

OTLP-only, like messages: the enricher's ``insert_agent_runs_batch`` is the one
writer. Column order, row shape and the startup schema guard live here.
"""

from __future__ import annotations

from collections.abc import Sequence

from clickhouse_connect.driver.client import Client

from hexgate_api.core.clickhouse import (
    BatchItem,
    insert_batch,
    verify_written_columns,
)
from hexgate_api.schemas import AgentRunEvent

AGENT_RUN_TABLE = "agent_run"

# Order matches schema.sql; received_at absent (server-stamped via column default).
_AGENT_RUN_COLUMNS = [
    "event_id",
    "occurred_at",
    "project_id",
    "agent_name",
    "agent_version_id",
    "session_id",
    "user_id",
    "run_id",
]


def verify_schema(client: Client) -> None:
    """Raise SchemaOutOfDate if agent_run misses a written column — an absent
    table (migration 0004 skipped) counts as every column missing."""
    verify_written_columns(client, ((AGENT_RUN_TABLE, _AGENT_RUN_COLUMNS),))


def _agent_run_row(
    event: AgentRunEvent, *, project_id: str, agent_version_id: str
) -> list:
    return [
        event.event_id,
        event.occurred_at,
        project_id,  # bearer-resolved
        event.agent_name,
        agent_version_id,  # platform-resolved
        event.session_id,
        event.user_id,
        event.run_id,
    ]


def insert_agent_runs_batch(
    clickhouse_client: Client,
    items: Sequence[BatchItem[AgentRunEvent]],
) -> None:
    """Write many agent_run rows in one batch insert. Same retry-safe,
    not-atomic contract as ``insert_llm_messages_batch``."""
    insert_batch(
        clickhouse_client,
        AGENT_RUN_TABLE,
        _AGENT_RUN_COLUMNS,
        _agent_run_row,
        items,
    )
