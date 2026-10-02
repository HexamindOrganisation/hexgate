"""Loading, validating and dry-running a policy (`policy.py`), on synthetic workspaces."""

from __future__ import annotations

import pytest

from evals.policy_writing.policy import (
    CaseError,
    decide,
    effective_policy,
    outcome,
    policy_columns,
)
from hexgate.security.decision import DecisionOutcome
from tests.evals.helpers import (
    AGENT,
    PERMISSIVE_DEFAULT,
    make_modules_workspace,
    make_workspace,
    write_module,
)


@pytest.mark.parametrize(
    ("role", "amount", "expected"),
    [
        ("billing", 500, DecisionOutcome.ALLOW),
        ("billing", 501, DecisionOutcome.DENY),
        ("support", 10, DecisionOutcome.NEEDS_APPROVAL),
    ],
)
def test_decide_happy_path(tmp_path, role, amount, expected) -> None:
    policy, _ = effective_policy(make_workspace(tmp_path))
    call = {"tool": "refund_order", "args": {"order_id": "o1", "amount": amount}}
    verdict = decide(policy, role, call)
    assert verdict.outcome == expected, verdict.reason


def test_when_the_role_is_undefined_then_decide_raises(tmp_path) -> None:
    # Not the `default` fallback: a case naming a role the policy lacks fails.
    policy, _ = effective_policy(make_workspace(tmp_path))
    with pytest.raises(CaseError, match="suport"):
        decide(policy, "suport", {"tool": "view_orders"})


def test_outcome_happy_path() -> None:
    assert outcome("approval_required") == DecisionOutcome.NEEDS_APPROVAL


def test_effective_policy_happy_path(tmp_path) -> None:
    policy, problems = effective_policy(make_workspace(tmp_path))
    assert problems == []
    assert policy is not None


def test_when_a_lint_warns_then_effective_policy_fails(tmp_path) -> None:
    policy, problems = effective_policy(make_workspace(tmp_path, PERMISSIVE_DEFAULT))
    assert policy is None
    assert any("permissive-default" in p for p in problems)


def test_when_policy_yaml_is_empty_then_it_is_an_empty_policy(tmp_path) -> None:
    # As `hexgate policy validate` reads it: valid, and every call denied.
    policy, problems = effective_policy(make_workspace(tmp_path, "# nothing yet\n"))
    assert problems == []
    verdict = decide(policy, "default", {"tool": "view_orders"})
    assert verdict.outcome == DecisionOutcome.DENY


def test_effective_policy_happy_path_on_a_module_tree(tmp_path) -> None:
    roles = "  default: [read_only]\n  billing: [read_only, payments]\n"
    policy, problems = effective_policy(make_modules_workspace(tmp_path, roles))
    assert problems == []
    refund = {"tool": "refund_order", "args": {"amount": 1001}}
    verdict = decide(policy, "billing", refund)
    assert verdict.outcome == DecisionOutcome.DENY  # the boundary's cap


def test_when_the_agent_has_its_own_column_then_effective_policy_resolves_it(
    tmp_path,
) -> None:
    roles = '  billing:\n    "*": [read_only]\n    shop-bot: [read_only, payments]\n'
    ws = make_modules_workspace(tmp_path, roles)
    refund = {"tool": "refund_order", "args": {"amount": 5}}
    for agent, expected in [
        (AGENT, DecisionOutcome.ALLOW),
        ("ops-bot", DecisionOutcome.DENY),
    ]:
        policy, _ = effective_policy(ws, agent)
        assert decide(policy, "billing", refund).outcome == expected


def test_when_a_module_tree_has_a_permissive_default_then_effective_policy_fails(
    tmp_path,
) -> None:
    # `policy check` doesn't lint the composed roles; only the resolved policy
    # shows that `default` grants what no named role does.
    roles = "  default: [read_only, payments]\n  billing: [read_only]\n"
    _, problems = effective_policy(make_modules_workspace(tmp_path, roles))
    assert any("permissive-default" in p for p in problems)


def test_when_a_module_tree_has_a_dead_grant_then_effective_policy_fails(
    tmp_path,
) -> None:
    # A module lint: the boundary denies what a capability grants.
    roles = "  default: [read_only]\n  billing: [read_only, payments]\n"
    ws = make_modules_workspace(tmp_path, roles)
    write_module(
        ws, "boundaries/no_views.yaml", "tools:\n  view_orders: { mode: deny }\n"
    )
    _, problems = effective_policy(ws)
    assert any("dead-grant" in p for p in problems)


def test_policy_columns_happy_path(tmp_path) -> None:
    roles = '  billing:\n    "*": [read_only]\n    ops-bot: [read_only, payments]\n'
    ws = make_modules_workspace(tmp_path, roles)
    policy, _ = effective_policy(ws)
    columns, problems = policy_columns(ws, None, policy)
    assert (set(columns), problems) == ({"*", "ops-bot"}, [])


def test_when_the_case_names_an_agent_then_policy_columns_is_its_policy(
    tmp_path,
) -> None:
    roles = '  billing:\n    "*": [read_only]\n    ops-bot: [read_only, payments]\n'
    ws = make_modules_workspace(tmp_path, roles)
    policy, _ = effective_policy(ws, AGENT)
    assert policy_columns(ws, AGENT, policy) == ({AGENT: policy}, [])


def test_when_an_agents_column_is_invalid_then_policy_columns_says_why(
    tmp_path,
) -> None:
    roles = '  default:\n    "*": [read_only]\n    ops-bot: [read_only, payments]\n'
    ws = make_modules_workspace(tmp_path, roles + "  billing: [read_only]\n")
    policy, _ = effective_policy(ws)
    columns, problems = policy_columns(ws, None, policy)
    # None, rather than the valid ones: a column left out would pass unrun.
    assert columns == {}
    assert problems[0].startswith("column ops-bot:")
