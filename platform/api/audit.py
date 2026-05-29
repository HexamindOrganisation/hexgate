"""Persistence layer for audit events.

Owns everything storage-shaped about the policy_decision audit log:
table name, column order, insert settings, row-tuple construction,
byte-size caps on the payload columns, and JSON serialization of the
dict-shaped fields.

The HTTP layer (main.py) calls insert_decision and maps custom
exceptions to status codes — nothing in this module knows about HTTP.
"""
from __future__ import annotations

import json

from clickhouse_connect.driver.client import Client

from schemas import DecisionEvent


class AuditPayloadTooLarge(Exception):
    """A dict field exceeds its serialized byte cap.

    Carries the offending field name and the configured limit so callers
    can render a useful diagnostic without re-deriving them.
    """

    def __init__(self, field: str, limit: int) -> None:
        super().__init__(f"{field} exceeds {limit} bytes")
        self.field = field
        self.limit = limit


# Per-field caps on the serialized JSON for the dict-shaped columns.
# Module constants rather than Settings entries — they're shape-of-the-
# protocol limits, not environment-overridable knobs.
MAX_ARGS_BYTES = 8 * 1024
MAX_HINT_BYTES = 4 * 1024

# Column order matches platform/clickhouse/init/schema.sql. `received_at`
# is intentionally absent — ClickHouse stamps it via the column default,
# keeping the server's clock as the source of truth.
_DECISION_COLUMNS = [
    "event_id", "occurred_at",
    "project_id", "agent_name", "agent_version_id",
    "session_id", "user_id",
    "tool_name", "role", "outcome", "error_type",
    "reason", "violations", "hint", "arguments",
]

# async_insert: ClickHouse buffers inserts server-side and flushes in
# bigger physical writes — fast insert, ~1s dashboard read-after-write
# lag. async_insert_deduplicate catches retried-identical inserts.
_DECISION_INSERT_SETTINGS = {
    "async_insert":             1,
    "wait_for_async_insert":    0,
    "async_insert_deduplicate": 1,
}


def insert_decision(
    clickhouse_client: Client,
    *,
    event: DecisionEvent,
    project_id: str,
    agent_version_id: str,
) -> None:
    """Write one decision row to the policy_decision table.

    ``project_id`` and ``agent_version_id`` are both server-resolved by
    the caller and passed in here — they are never read from the event
    body. (The HTTP layer derives ``project_id`` from the bearer and
    looks up ``agent_version_id`` from the relational store.)

    Raises:
        AuditPayloadTooLarge: serialized ``arguments`` or ``hint``
            exceeds its per-field cap. The caller chooses how to surface
            this — e.g. HTTP 413.
        clickhouse_connect.driver.exceptions.ClickHouseError: propagated
            unchanged from the driver on insert failure. The caller maps
            it to the transport-appropriate error shape — e.g. HTTP 503.
    """
    args_json = json.dumps(event.arguments, default=str) if event.arguments is not None else ""
    hint_json = json.dumps(event.hint, default=str) if event.hint is not None else ""
    if len(args_json.encode("utf-8")) > MAX_ARGS_BYTES:
        raise AuditPayloadTooLarge("arguments", MAX_ARGS_BYTES)
    if len(hint_json.encode("utf-8")) > MAX_HINT_BYTES:
        raise AuditPayloadTooLarge("hint", MAX_HINT_BYTES)

    row = [
        event.event_id,
        event.occurred_at,
        project_id,                # server-resolved from bearer
        event.agent_name,
        agent_version_id,          # server-resolved from relational store
        event.session_id,
        event.user_id,
        event.tool_name,
        event.role,
        event.outcome,
        event.error_type,
        event.reason,
        list(event.violations),
        hint_json,
        args_json,
    ]
    clickhouse_client.insert(
        "policy_decision",
        [row],
        column_names=_DECISION_COLUMNS,
        settings=_DECISION_INSERT_SETTINGS,
    )
