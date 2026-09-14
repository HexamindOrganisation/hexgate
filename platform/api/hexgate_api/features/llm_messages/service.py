"""ClickHouse read/write paths for LLM message rows (scope ``hexgate.messages``).

Batch insert only on the write side: unlike decisions and usage there is no
HTTP ingest for messages — the OTLP pipeline is the only way in, so the
enricher's ``insert_llm_messages_batch`` is the one writer. The read side is
``list_llm_messages``, the session transcript behind ``router.py``'s
endpoint. Column order, row shape and the startup schema guard live here.
"""

from __future__ import annotations

from collections.abc import Sequence

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


def list_llm_messages(
    client: Client,
    *,
    project_id: str,
    session_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """One session's transcript rows, oldest first. Returns ``{rows, total,
    limit, offset}`` with ``total`` the unpaginated match count.

    ``session_id`` is required, not another optional filter: this table's rows
    are whole prompts, so an unscoped read would stream a project's entire
    message history.

    The session IS the scope — there is no window parameter, unlike every
    other read here. ``scope_filters`` always emits a time predicate, so this
    passes the retention horizon, past which nothing is stored anyway. A
    dashboard window would silently cut the head off a conversation that
    started before it, and the cut is unreadable: the surviving rows begin at
    a nonzero ``message_seq``, which schema.sql tells a reader means the
    pipeline lost a row. Paging, not a narrower window, is how a long
    transcript is taken in pieces.

    Ordered ``(occurred_at, message_seq, event_id)`` — exactly the storage
    sort key after project/session, so the scan is already in output order.
    Ascending, unlike the newest-first decision list: a transcript is read
    forwards. ``message_seq`` cannot order on its own (it restarts at 0 for
    each ``turn_key``), and ``occurred_at`` cannot either (DateTime64(3) ties
    within a millisecond), so both run ahead of ``event_id``, which breaks the
    remaining ties into the total order paging needs.

    Reads without ``FINAL``, like every other read path here: after a
    whole-batch retry both copies of an ``event_id`` are returned — adjacent,
    since the sort key ends in ``event_id`` — and counted in ``total``, until
    ReplacingMergeTree merges the parts. A repeated message in a transcript is
    that window, not the agent saying the same thing twice.
    """
    where, params = scope_filters(project_id, RETENTION_HOURS)
    where.append("session_id = {session_id:String}")
    params["session_id"] = session_id
    where_sql = " AND ".join(where)

    # Same one-scan page+total trick as list_decisions: count() OVER () is
    # computed before LIMIT, so each row carries the full match count.
    page_params = {**params, "lim": limit, "off": offset}
    result = client.query(
        f"SELECT {_LIST_COLUMNS}, count() OVER () AS total_matches "
        f"FROM {LLM_MESSAGE_TABLE} WHERE {where_sql} "
        "ORDER BY occurred_at, message_seq, event_id "
        "LIMIT {lim:UInt32} OFFSET {off:UInt32}",
        parameters=page_params,
    )
    rows = []
    total = 0
    for raw in result.result_rows:
        row = dict(zip(result.column_names, raw))
        total = int(row.pop("total_matches"))
        for column in ("input_messages", "output_messages", "system_instructions"):
            row[column] = decode_json_column(row.get(column) or "")
        # The zero UUID is the column's "no run" value, not a run to join on.
        if row.get("run_id") == ZERO_RUN_ID:
            row["run_id"] = None
        rows.append(row)

    # An empty page past the end (offset > 0) carries no window value, so the
    # match count is unavailable; fall back to a plain count for that rare case.
    if not rows and offset:
        total = int(
            client.query(
                f"SELECT count() FROM {LLM_MESSAGE_TABLE} WHERE {where_sql}",
                parameters=params,
            ).result_rows[0][0]
        )

    return {"rows": rows, "total": total, "limit": limit, "offset": offset}
