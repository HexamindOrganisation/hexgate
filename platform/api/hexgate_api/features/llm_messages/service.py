"""ClickHouse write path for LLM message rows (scope ``hexgate.messages``).

Batch insert only: unlike decisions and usage there is no HTTP ingest for
messages — the OTLP pipeline is the only way in, so the enricher's
``insert_llm_messages_batch`` is the one writer. The session-scoped read
endpoint lands in its own change (``router.py``), which keeps this module
the table contract: column order, row shape and the startup schema guard.
"""

from __future__ import annotations

from collections.abc import Sequence

from clickhouse_connect.driver.client import Client

from hexgate_api.core.clickhouse import (
    ZERO_RUN_ID,
    BatchItem,
    insert_batch,
    verify_written_columns,
)
from hexgate_api.schemas import LlmMessageEvent

LLM_MESSAGE_TABLE = "llm_message"

# Order matches schema.sql; received_at absent (server-stamped via column default).
_LLM_MESSAGE_COLUMNS = [
    "event_id",
    "occurred_at",
    "project_id",
    "agent_name",
    "agent_version_id",
    "session_id",
    "user_id",
    "model",
    "turn_key",
    "message_seq",
    "resynced",
    "truncated",
    "input_messages",
    "output_messages",
    "system_instructions",
    "run_id",
]


def verify_schema(client: Client) -> None:
    """Raise SchemaOutOfDate if llm_message misses a written column.

    This feature's slice of the startup guard — same machinery and semantics
    as the audit tables' check (see core.clickhouse.verify_written_columns).
    An absent table counts as every column missing, which is the case this
    check exists for: the table ships as a hand-applied migration
    (platform/clickhouse/migrations/0003_add_llm_message.sql), and a stage
    that skipped it must refuse to boot rather than halt the enricher on its
    first message span."""
    verify_written_columns(client, ((LLM_MESSAGE_TABLE, _LLM_MESSAGE_COLUMNS),))


def _llm_message_row(
    event: LlmMessageEvent, *, project_id: str, agent_version_id: str
) -> list:
    """Build one llm_message row in ``_LLM_MESSAGE_COLUMNS`` order.

    The two flags are stored as UInt8, so they go out as ints rather than
    leaving the bool→UInt8 coercion to the driver.
    """
    return [
        event.event_id,
        event.occurred_at,
        project_id,  # bearer-resolved
        event.agent_name,
        agent_version_id,  # platform-resolved
        event.session_id,
        event.user_id,
        event.model,
        event.turn_key,
        event.message_seq,
        int(event.resynced),
        int(event.truncated),
        event.input_messages,
        event.output_messages,
        event.system_instructions,
        event.run_id or ZERO_RUN_ID,
    ]


def insert_llm_messages_batch(
    clickhouse_client: Client,
    items: Sequence[BatchItem[LlmMessageEvent]],
) -> None:
    """Write many llm_message rows in one batch insert.

    Each ``BatchItem`` carries its own ``project_id`` and ``agent_version_id``
    (keyword-only, see ``BatchItem``) — resolved per item, because a consumer
    batch aggregates across Kafka records and so can span projects and agents.
    Retry-safe rather than atomic, with the same guarantee edges as
    ``insert_llm_invocations_batch``: a failed call can have landed part of
    the batch, the caller retries the whole batch, and
    ReplacingMergeTree(received_at) collapses re-inserted event_ids on a
    background merge (both copies visible to non-FINAL reads until then;
    dedup stays within the monthly received_at partition). No async_insert —
    see ``BATCH_INSERT_SETTINGS`` in core/clickhouse.py.
    """
    insert_batch(
        clickhouse_client,
        LLM_MESSAGE_TABLE,
        _LLM_MESSAGE_COLUMNS,
        _llm_message_row,
        items,
    )
