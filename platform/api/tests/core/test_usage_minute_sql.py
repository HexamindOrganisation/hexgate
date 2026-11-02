"""The usage rollups' SQL, pinned without a database.

The decision views count what run.tool_calls / run.denials count, which is
defined by which keys the SDK decides outside the guard runner. A renamed SDK
key would silently fold those decisions back into the counts.

usage_minute_user is usage_minute rekeyed by user_id with anonymous rows
filtered out (README G12). Any other difference is drift.
"""

from __future__ import annotations

from typing import Final

import pytest

from hexgate.security.models import AGENT_REACH_PREFIXES, AGENT_RUN_TOOL
from hexgate.security.network import EGRESS_TOOL_ARGS
from tests.core.test_migrations import (
    CLICKHOUSE_INIT_SCHEMA,
    USAGE_BACKFILL,
    USER_USAGE_BACKFILL,
    split_statements,
)

HANDOFF_PREFIX = "agent.handoff:"
TOOL_REACH_PREFIX = "agent.tool:"
EGRESS_PREFIX = "net."

DECISION_FILTER = (
    f"tool_name != '{AGENT_RUN_TOOL}'",
    f"NOT startsWith(tool_name, '{HANDOFF_PREFIX}')",
    f"NOT startsWith(tool_name, '{EGRESS_PREFIX}')",
)

AGENT_ROLLUP_TABLE: Final = "hexgate_audit.usage_minute"
USER_ROLLUP_NAME: Final = "usage_minute_user"
USER_ROLLUP_SUBSTITUTIONS: Final = (
    (AGENT_ROLLUP_TABLE, f"hexgate_audit.{USER_ROLLUP_NAME}"),
    ("agent_name LowCardinality(String)", "user_id String"),
    ("agent_name", "user_id"),
    # G4, views
    (
        "FROM hexgate_audit.agent_run GROUP BY",
        "FROM hexgate_audit.agent_run WHERE user_id != '' GROUP BY",
    ),
    (
        "FROM hexgate_audit.policy_decision WHERE",
        "FROM hexgate_audit.policy_decision WHERE user_id != '' AND",
    ),
    (
        "FROM hexgate_audit.llm_invocation GROUP BY",
        "FROM hexgate_audit.llm_invocation WHERE user_id != '' GROUP BY",
    ),
    # G4, backfill (each branch's received_at bound)
    ("INTERVAL 30 DAY", "INTERVAL 30 DAY AND user_id != ''"),
)


def _schema_statements() -> list[str]:
    return split_statements(CLICKHOUSE_INIT_SCHEMA.read_text())


def _view(name: str) -> str:
    (statement,) = [
        statement for statement in _schema_statements() if f".{name}\n" in statement
    ]
    return statement


def _normalized(statement: str) -> str:
    return " ".join(statement.split())  # split_statements already drops comments


def _as_user_rollup(statement: str) -> str:
    text = _normalized(statement)
    for old, new in USER_ROLLUP_SUBSTITUTIONS:
        text = text.replace(old, new)
    return text


def _single_statement(sql: str) -> str:
    (statement,) = split_statements(sql)
    return statement


def test_the_filtered_keys_are_the_sdk_keys() -> None:
    assert HANDOFF_PREFIX in AGENT_REACH_PREFIXES
    assert TOOL_REACH_PREFIX in AGENT_REACH_PREFIXES
    assert all(key.startswith(EGRESS_PREFIX) for key in EGRESS_TOOL_ARGS)


@pytest.mark.parametrize(
    "sql",
    [
        _view("usage_minute_from_decisions"),
        USAGE_BACKFILL.read_text(),
        _view("usage_minute_user_from_decisions"),
        USER_USAGE_BACKFILL.read_text(),
    ],
    ids=["view", "backfill", "user-view", "user-backfill"],
)
def test_only_keys_decided_outside_the_guard_runner_are_dropped(sql: str) -> None:
    for clause in DECISION_FILTER:
        assert clause in sql
    # agent-as-tool reach goes through the guard runner and is counted in run.*.
    assert TOOL_REACH_PREFIX not in sql


def test_the_user_rollup_is_the_agent_rollup_rekeyed() -> None:
    statements = _schema_statements()
    agent = [
        s for s in statements if AGENT_ROLLUP_TABLE in s and USER_ROLLUP_NAME not in s
    ]
    user = [s for s in statements if USER_ROLLUP_NAME in s]

    assert len(agent) == len(user) == 4
    assert [_as_user_rollup(a) for a in agent] == [_normalized(u) for u in user]


def test_the_user_backfill_is_the_agent_backfill_rekeyed() -> None:
    agent = _single_statement(USAGE_BACKFILL.read_text())
    user = _single_statement(USER_USAGE_BACKFILL.read_text())

    assert _as_user_rollup(agent) == _normalized(user)
