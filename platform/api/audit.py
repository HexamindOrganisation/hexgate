"""Persistence layer for audit events. HTTP-agnostic — exceptions map to status codes in main.py."""
from __future__ import annotations

import json

from clickhouse_connect.driver.client import Client

from schemas import DecisionEvent


class AuditPayloadTooLarge(Exception):
    """Serialized payload column exceeds its per-field byte cap."""

    def __init__(self, field: str, limit: int) -> None:
        super().__init__(f"{field} exceeds {limit} bytes")
        self.field = field
        self.limit = limit


MAX_ARGS_BYTES = 8 * 1024
MAX_HINT_BYTES = 4 * 1024

# Order matches schema.sql; received_at absent (server-stamped via column default).
_DECISION_COLUMNS = [
    "event_id", "occurred_at",
    "project_id", "agent_name", "agent_version_id",
    "session_id", "user_id",
    "tool_name", "role", "outcome", "error_type",
    "reason", "violations", "hint", "arguments",
]

# async_insert lets ClickHouse buffer + batch; ~1s read-after-write lag.
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
    """Write one decision row to policy_decision.

    Raises AuditPayloadTooLarge on payload overflow and ClickHouseError on
    insert failure; both propagate so the caller maps them to transport errors.
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
        project_id,                # bearer-resolved
        event.agent_name,
        agent_version_id,          # platform-resolved
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
