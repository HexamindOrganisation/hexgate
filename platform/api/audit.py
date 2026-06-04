"""Persistence layer for audit events. HTTP-agnostic — exceptions map to status codes in main.py."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from clickhouse_connect.driver.client import Client

from schemas import DecisionEvent


class AuditPayloadTooLarge(Exception):
    """Serialized payload column exceeds its per-field byte cap."""

    def __init__(self, field: str, limit: int) -> None:
        super().__init__(f"{field} exceeds {limit} bytes")
        self.field = field
        self.limit = limit


class AuditEventOutOfWindow(Exception):
    """occurred_at falls outside the accepted ingest window."""


MAX_ARGS_BYTES = 8 * 1024
MAX_HINT_BYTES = 4 * 1024

# Accepted occurred_at window: small future skew for client clocks, and no
# older than retention — rows past TTL would be merged away on arrival.
CLOCK_SKEW_FUTURE = timedelta(minutes=5)
RETENTION_WINDOW = timedelta(days=90)


def validate_event_window(occurred_at: datetime) -> None:
    """Raise :class:`AuditEventOutOfWindow` when occurred_at is outside
    [now - retention, now + skew]. Mapped to 400 in main.py."""
    now = datetime.now(timezone.utc)
    if occurred_at > now + CLOCK_SKEW_FUTURE:
        raise AuditEventOutOfWindow("occurred_at is in the future")
    if occurred_at < now - RETENTION_WINDOW:
        raise AuditEventOutOfWindow("occurred_at is older than retention window")

# Order matches schema.sql; received_at absent (server-stamped via column default).
_DECISION_COLUMNS = [
    "event_id", "occurred_at",
    "project_id", "agent_name", "agent_version_id",
    "session_id", "user_id",
    "tool_name", "role", "outcome", "error_type",
    "reason", "violations", "hint", "arguments",
]

# async_insert batches small inserts; wait_for_async_insert=1 blocks until flush
# so write failures surface synchronously — an audit log must not ack-then-drop.
# Retry dedup is NOT handled here: insert-level dedup settings no-op on
# non-replicated tables. The ReplacingMergeTree(received_at) engine collapses
# duplicate event_ids on background merges instead (see schema.sql).
_DECISION_INSERT_SETTINGS = {
    "async_insert":          1,
    "wait_for_async_insert": 1,
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
