"""A seeded slice of the three event tables, and a ClickHouse client that
serves it.

The fake dispatches on the SQL text rather than on call order, so a test keeps
asserting about the same seeded rows if the service reorders its reads — and a
query the service starts issuing that nothing here recognises fails loudly
instead of silently receiving another query's rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

# Fixed ids and instants so the golden annex is byte-stable.
PERIOD_START = datetime(2026, 3, 21, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 9, 17, tzinfo=timezone.utc)
EARLIEST = datetime(2026, 4, 2, 9, 15, tzinfo=timezone.utc)
LATEST = datetime(2026, 9, 16, 18, 40, tzinfo=timezone.utc)

DECISION_EVENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
APPROVAL_EVENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
BAN_EVENT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
BAN_EVENT_ID_2 = uuid.UUID("44444444-4444-4444-4444-444444444444")

# Headline figures the seeded rows add up to; tests assert against these names
# rather than repeating the literals.
DECISIONS = 12
ALLOWS = 7
DENIALS = 3
APPROVALS_REQUIRED = 2
GUARD_REFUSALS = 2
DECISIONS_WITHOUT_RUN = 5
BAN_ENFORCEMENTS = 2
MODEL_CALLS = 6
MODEL_CALL_ERRORS = 1

_DECISION_LIST_COLUMNS = [
    "event_id",
    "occurred_at",
    "received_at",
    "agent_name",
    "agent_version_id",
    "session_id",
    "user_id",
    "tool_name",
    "user_roles",
    "deciding_role",
    "outcome",
    "error_type",
    "reason",
    "violations",
    "hint",
    "arguments",
    "attributes",
    "total_matches",
]
_BAN_LIST_COLUMNS = [
    "event_id",
    "occurred_at",
    "received_at",
    "agent_name",
    "session_id",
    "user_id",
    "ban_type",
    "ban_id",
    "reason",
    "total_matches",
]


def _decision_list_row(event_id: uuid.UUID, outcome: str, *, total: int) -> list[Any]:
    return [
        event_id,
        LATEST,
        LATEST,
        "support_bot",
        "agv_1",
        "sess_1",
        "u_alice",
        "read_file",
        ["support"],
        "support",
        outcome,
        {"deny": "policy_denied", "needs_approval": "approval_required"}.get(
            outcome, ""
        ),
        "outside the allowed path" if outcome == "deny" else "",
        ["path_not_allowed"] if outcome == "deny" else [],
        "",
        '{"path": "/etc/passwd", "api_key": "[REDACTED]"}',
        "",
        total,
    ]


_BAN_LIST_ROWS = [
    [
        BAN_EVENT_ID,
        LATEST,
        LATEST,
        "support_bot",
        "sess_9",
        "u_mallory",
        "user",
        "ban_abc123",
        "offboarded",
        BAN_ENFORCEMENTS,
    ],
    [
        BAN_EVENT_ID_2,
        EARLIEST,
        EARLIEST,
        "support_bot",
        "sess_4",
        "u_alice",
        "agent",
        "ban_def456",
        "paused during an incident",
        BAN_ENFORCEMENTS,
    ],
]


class _Result:
    def __init__(self, rows: list[Any], columns: list[str] | None = None) -> None:
        self.result_rows = rows
        self.column_names = columns or []


class FakeClickHouse:
    """Serves the seeded slice; records every statement it was asked for."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.parameters: list[dict[str, Any]] = []
        self.settings: list[dict[str, Any]] = []

    def query(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> _Result:
        self.statements.append(sql)
        self.parameters.append(dict(parameters or {}))
        self.settings.append(dict(settings or {}))
        return _Result(*self._rows_for(sql, parameters or {}))

    def _rows_for(
        self, sql: str, params: dict[str, Any]
    ) -> tuple[list[Any], list[str] | None]:
        if "guard_refusals" in sql:
            return [
                [
                    DECISIONS,
                    GUARD_REFUSALS,
                    DECISIONS_WITHOUT_RUN,
                    EARLIEST,
                    LATEST,
                ]
            ], None
        if "AS errors" in sql:
            return [[MODEL_CALLS, MODEL_CALL_ERRORS, EARLIEST, LATEST]], None
        if "min(received_at)" in sql and "ban_enforcement" in sql:
            return [[BAN_ENFORCEMENTS, EARLIEST, LATEST]], None
        if "arrayJoin" in sql:
            return [
                ["support", "allow", 7],
                ["support", "deny", 3],
                ["support", "needs_approval", 2],
            ], None
        if "GROUPING SETS" in sql and "policy_decision" in sql:
            return [
                # (agent, tool, user, outcome, g_agent, g_tool, g_user, g_outcome, n)
                ["", "", "", "", 1, 1, 1, 1, DECISIONS],
                ["", "", "", "allow", 1, 1, 1, 0, ALLOWS],
                ["", "", "", "deny", 1, 1, 1, 0, DENIALS],
                ["", "", "", "needs_approval", 1, 1, 1, 0, APPROVALS_REQUIRED],
                ["support_bot", "", "", "allow", 0, 1, 1, 0, 7],
                ["support_bot", "", "", "deny", 0, 1, 1, 0, 3],
                ["support_bot", "", "", "needs_approval", 0, 1, 1, 0, 2],
                ["", "read_file", "", "allow", 1, 0, 1, 0, 7],
                ["", "", "u_alice", "allow", 1, 1, 0, 0, 7],
            ], None
        if "GROUPING SETS" in sql and "llm_invocation" in sql:
            return [
                # (model, agent, user, g_model, g_agent, g_user, calls, in, out)
                ["", "", "", 1, 1, 1, MODEL_CALLS, 600, 300],
                ["claude-sonnet-5", "", "", 0, 1, 1, 4, 400, 200],
                ["gpt-4o", "", "", 0, 1, 1, 2, 200, 100],
                ["", "support_bot", "", 1, 0, 1, MODEL_CALLS, 600, 300],
                ["", "", "u_alice", 1, 1, 0, MODEL_CALLS, 600, 300],
            ], None
        if "ORDER BY occurred_at DESC" in sql and "ban_enforcement" in sql:
            return [list(row) for row in _BAN_LIST_ROWS], _BAN_LIST_COLUMNS
        if "ORDER BY occurred_at DESC" in sql and "policy_decision" in sql:
            if params.get("outcome") == "needs_approval":
                return [
                    _decision_list_row(
                        APPROVAL_EVENT_ID,
                        "needs_approval",
                        total=APPROVALS_REQUIRED,
                    )
                ], _DECISION_LIST_COLUMNS
            return [
                # The same event twice: a retried send the store has not yet
                # merged away, which the report must not show twice.
                _decision_list_row(DECISION_EVENT_ID, "deny", total=DECISIONS),
                _decision_list_row(DECISION_EVENT_ID, "deny", total=DECISIONS),
            ], _DECISION_LIST_COLUMNS
        raise AssertionError(f"unseeded query: {sql}")
