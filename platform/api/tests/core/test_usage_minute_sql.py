"""usage_minute's decision filter, pinned to the SDK keys it names. No database.

The view counts what run.tool_calls / run.denials count, which is defined by
which keys the SDK decides outside the guard runner. A renamed SDK key would
silently fold those decisions back into the counts.
"""

from __future__ import annotations

import pytest

from hexgate.security.models import AGENT_REACH_PREFIXES, AGENT_RUN_TOOL
from hexgate.security.network import EGRESS_TOOL_ARGS
from tests.core.test_migrations import (
    CLICKHOUSE_INIT_SCHEMA,
    USAGE_BACKFILL,
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


def _decisions_view() -> str:
    (statement,) = [
        statement
        for statement in split_statements(CLICKHOUSE_INIT_SCHEMA.read_text())
        if "usage_minute_from_decisions" in statement
    ]
    return statement


def test_the_filtered_keys_are_the_sdk_keys() -> None:
    assert HANDOFF_PREFIX in AGENT_REACH_PREFIXES
    assert TOOL_REACH_PREFIX in AGENT_REACH_PREFIXES
    assert all(key.startswith(EGRESS_PREFIX) for key in EGRESS_TOOL_ARGS)


@pytest.mark.parametrize(
    "sql",
    [_decisions_view(), USAGE_BACKFILL.read_text()],
    ids=["view", "backfill"],
)
def test_only_keys_decided_outside_the_guard_runner_are_dropped(sql: str) -> None:
    for clause in DECISION_FILTER:
        assert clause in sql
    # agent-as-tool reach goes through the guard runner and is counted in run.*.
    assert TOOL_REACH_PREFIX not in sql
