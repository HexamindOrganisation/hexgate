"""ClickHouse read/write paths for LLM message rows (scope ``hexgate.messages``).

Batch insert only on the write side: unlike decisions and usage there is no
HTTP ingest for messages — the OTLP pipeline is the only way in, so the
enricher's ``insert_llm_messages_batch`` is the one writer. The read side is
``list_llm_messages``, the session transcript behind ``router.py``'s
endpoint. Column order, row shape and the startup schema guard live here.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from clickhouse_connect.driver.client import Client

from hexgate_api.core.clickhouse import (
    ZERO_RUN_ID,
    BatchItem,
    decode_json_column,
    insert_batch,
    verify_written_columns,
)
from hexgate_api.query_scope import RETENTION_HOURS, scope_filters
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


# received_at joins the written columns here: a reader needs to tell when a
# row landed apart from when the call happened (the gap is the pipeline's).
_LIST_COLUMNS = (
    "event_id, occurred_at, received_at, agent_name, agent_version_id, "
    "session_id, user_id, model, turn_key, message_seq, resynced, truncated, "
    "input_messages, output_messages, system_instructions, run_id"
)

# Deliberately below the 200 the decision reads allow: a decision row is about
# a kilobyte, while one message row carries up to ~272 KiB of capped content,
# so the same page size would be a two-order-of-magnitude bigger response.
MAX_PAGE_SIZE = 100


class NoMessageScope(ValueError):
    """A transcript read named neither a session nor a run."""

    def __init__(self) -> None:
        super().__init__("session_id or run_id is required")


def list_llm_messages(
    client: Client,
    *,
    project_id: str,
    session_id: str | None = None,
    run_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """One transcript's rows, oldest first. Returns ``{rows, total, limit,
    offset}`` with ``total`` the unpaginated match count.

    Scoped by ``session_id``, ``run_id`` or both — at least one, else
    :class:`NoMessageScope`, since a row here is a whole prompt and an
    unscoped read would stream the project's entire message history. Two
    scopes because ``session_id`` is caller-supplied and usually unset (it
    defaults to ``""`` all the way down; see ``AuditEnvelope``), which would
    otherwise leave those transcripts stored and unreadable for their whole
    TTL. A zero ``run_id`` is no more a scope than a blank session: it is the
    column's "outside any run" value, shared across the project.

    No window parameter, unlike every other read here — a time filter on a
    conversation can only cut its head off, and the cut reads as a
    ``message_seq`` gap, which schema.sql defines as a row the pipeline lost.
    Long transcripts page instead.

    Ordered to match the storage sort key after project/session, so the scan
    reads in order and stops at ``limit + offset`` rows instead of sorting the
    whole match; see schema.sql for why that key is shaped this way.
    Ascending, unlike the newest-first decision list: a transcript is read
    forwards. A run-scoped read gets no pruning past ``project_id``
    and scans the retention window — the cost of the session column being
    optional. Reads without ``FINAL``, so a retried batch shows both copies
    of an ``event_id`` until they merge (see ``insert_llm_messages_batch``).
    """
    if not session_id and (run_id is None or run_id == ZERO_RUN_ID):
        raise NoMessageScope()

    where, params = scope_filters(project_id, RETENTION_HOURS)
    if session_id:
        where.append("session_id = {session_id:String}")
        params["session_id"] = session_id
    if run_id is not None and run_id != ZERO_RUN_ID:
        where.append("run_id = {run_id:UUID}")
        params["run_id"] = run_id
    where_sql = " AND ".join(where)

    # Two queries, NOT list_decisions' single scan with ``count() OVER ()``.
    # A window function has no frame to prune, so ClickHouse buffers every
    # matching row — content columns included — before emitting the first,
    # which throws away the read-in-order short-circuit this ORDER BY was
    # chosen for. Measured on a 2000-row session of 200 KB rows: 1.07 GiB and
    # 2000 rows read, against 44 MiB and 52 rows for the same page without
    # it. The separate count() touches no content column, and there is no
    # per-query memory cap here to turn the blow-up into a clean 503.
    page_params = {**params, "lim": limit, "off": offset}
    result = client.query(
        f"SELECT {_LIST_COLUMNS} FROM {LLM_MESSAGE_TABLE} WHERE {where_sql} "
        "ORDER BY occurred_at, message_seq, event_id "
        "LIMIT {lim:UInt32} OFFSET {off:UInt32}",
        parameters=page_params,
    )
    rows = []
    for raw in result.result_rows:
        row = dict(zip(result.column_names, raw))
        for column in ("input_messages", "output_messages", "system_instructions"):
            row[column] = decode_json_column(row.get(column) or "")
        # The zero UUID is the column's "no run" value, not a run to join on.
        if row.get("run_id") == ZERO_RUN_ID:
            row["run_id"] = None
        rows.append(row)

    total = int(
        client.query(
            f"SELECT count() FROM {LLM_MESSAGE_TABLE} WHERE {where_sql}",
            parameters=params,
        ).result_rows[0][0]
    )

    return {"rows": rows, "total": total, "limit": limit, "offset": offset}
