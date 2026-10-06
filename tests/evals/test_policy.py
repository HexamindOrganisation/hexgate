"""Loading, validating and dry-running a policy (`policy.py`), on synthetic workspaces."""

from __future__ import annotations

import datetime

import pytest

from evals.policy_writing.policy import CaseError, decide, effective_policy, outcome
from hexgate.security.decision import DecisionOutcome
from tests.evals.helpers import (
    AGENT,
    PERMISSIVE_DEFAULT,
    make_modules_workspace,
    make_workspace,
)

# Reach declared for handoff only, not for agent-as-tool.
HANDOFF_ONLY = """\
version: 1
roles:
  default:
    tools:
      view_orders: { mode: allow }
    agents:
      billing-bot: { mode: allow, via: [handoff] }
"""

DATED_CONSTRAINT = """\
version: 1
roles:
  default:
    tools:
      view_orders:
        mode: allow
        constraints: ['args.since >= "2026-01-01"', 'ctx.hired_on >= "2026-01-01"']
"""


# Roles disagreeing on a guard: one guard pipeline per agent can't serve both.
GUARD_DIVERGENCE = """\
version: 1
roles:
  default:
    guards:
      g: { enabled: false }
  admin:
    guards:
      g: { enabled: true }
"""


# Agent gates whose constraints read the args only the gate sends.
ADMIT_SHOP_BOT = """\
version: 1
roles:
  default:
    admission: { mode: allow, constraints: ['args.agent == "shop-bot"'] }
"""

HANDOFF_TO_BILLING = """\
version: 1
roles:
  default:
    agents:
      billing-bot:
        mode: allow
        constraints: ['args.target == "billing-bot"', 'args.via == "handoff"']
"""


# A reach rule that reads the call's own key from the run's tools.
HANDOFF_SEEN = """\
version: 1
roles:
  default:
    agents:
      billing-bot:
        mode: allow
        constraints: ['any(run.tools_used, . == "agent.handoff:billing-bot")']
"""


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


@pytest.mark.parametrize("tool", ["agent.run", "agent.handoff:ops-bot"])
def test_when_admission_or_handoff_is_not_declared_then_decide_allows(
    tmp_path, tool
) -> None:
    # As the runtime: the gate checks nothing, and the call goes through.
    policy, _ = effective_policy(make_workspace(tmp_path))
    assert decide(policy, "default", {"tool": tool}).outcome == DecisionOutcome.ALLOW


@pytest.mark.parametrize("tool", ["agent.tool:ops-bot", "skill:pdf"])
def test_when_a_tool_reach_or_skill_gate_is_not_declared_then_decide_raises(
    tmp_path, tool
) -> None:
    # The runtime decides such a call under the tool's own name instead.
    policy, _ = effective_policy(make_workspace(tmp_path))
    with pytest.raises(CaseError, match="dry-run that tool instead"):
        decide(policy, "default", {"tool": tool})


def test_when_a_gate_is_declared_then_decide_evaluates_it(tmp_path) -> None:
    policy, problems = effective_policy(make_workspace(tmp_path, HANDOFF_ONLY))
    assert problems == []
    handoff = decide(policy, "default", {"tool": "agent.handoff:ops-bot"})
    assert handoff.outcome == DecisionOutcome.DENY  # declared, and ops-bot unlisted
    with pytest.raises(CaseError, match="agent-as-tool reach"):
        decide(policy, "default", {"tool": "agent.tool:ops-bot"})


def test_when_a_call_holds_yaml_dates_then_decide_compares_them_as_text(
    tmp_path,
) -> None:
    # As `policy test --args` / `--attributes` (JSON) would give them.
    policy, problems = effective_policy(make_workspace(tmp_path, DATED_CONSTRAINT))
    assert problems == []
    february, december = datetime.date(2026, 2, 1), datetime.date(2025, 12, 1)
    call = {"tool": "view_orders", "args": {"since": february}}
    for hired_on, expected in [
        (february, DecisionOutcome.ALLOW),
        (december, DecisionOutcome.DENY),
    ]:
        dated = {**call, "attributes": {"hired_on": hired_on}}
        assert decide(policy, "default", dated).outcome == expected


def test_when_a_call_is_on_admission_then_decide_sends_the_agent(tmp_path) -> None:
    policy, problems = effective_policy(make_workspace(tmp_path, ADMIT_SHOP_BOT), AGENT)
    assert problems == []
    verdict = decide(policy, "default", {"tool": "agent.run"})
    assert verdict.outcome == DecisionOutcome.ALLOW, verdict.reason


@pytest.mark.parametrize(
    "tool", ["agent.handoff:billing-bot", "agent.handoff: billing-bot "]
)
def test_when_a_call_is_on_reach_then_decide_sends_the_trimmed_target_and_via(
    tmp_path, tool
) -> None:
    policy, problems = effective_policy(
        make_workspace(tmp_path, HANDOFF_TO_BILLING), AGENT
    )
    assert problems == []
    verdict = decide(policy, "default", {"tool": tool})
    assert verdict.outcome == DecisionOutcome.ALLOW, verdict.reason


def test_when_a_gate_call_carries_its_own_args_then_decide_raises(tmp_path) -> None:
    # The gate sends its own args, so a case's would be silently ignored.
    policy, _ = effective_policy(make_workspace(tmp_path, ADMIT_SHOP_BOT), AGENT)
    with pytest.raises(CaseError, match="gate sends its own args"):
        decide(policy, "default", {"tool": "agent.run", "args": {"agent": "ops-bot"}})


def test_when_a_reach_target_is_padded_then_run_facts_name_the_trimmed_key(
    tmp_path,
) -> None:
    policy, problems = effective_policy(make_workspace(tmp_path, HANDOFF_SEEN), AGENT)
    assert problems == []
    call = {"tool": "agent.handoff: billing-bot ", "run_facts": {"tool_calls": 1}}
    verdict = decide(policy, "default", call)
    assert verdict.outcome == DecisionOutcome.ALLOW, verdict.reason


def test_when_a_run_fact_key_is_not_a_string_then_decide_raises(tmp_path) -> None:
    policy, _ = effective_policy(make_workspace(tmp_path))
    with pytest.raises(CaseError):
        decide(policy, "default", {"tool": "view_orders", "run_facts": {1: 2}})


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


def test_when_the_policy_holds_a_yaml_date_then_effective_policy_fails(
    tmp_path,
) -> None:
    dated = "version: 1\nroles:\n  default:\n    consts:\n      cutoff: 2026-01-01\n"
    policy, problems = effective_policy(make_workspace(tmp_path, dated))
    assert policy is None
    assert problems[0].startswith("can't compile:")


def test_when_roles_disagree_on_guards_then_effective_policy_fails(tmp_path) -> None:
    # As `hexgate policy validate` rejects it.
    policy, problems = effective_policy(make_workspace(tmp_path, GUARD_DIVERGENCE))
    assert policy is None
    assert any("guard-divergence" in p for p in problems)


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
    (ws / "policies" / "boundaries" / "no_views.yaml").write_text(
        "tools:\n  view_orders: { mode: deny }\n"
    )
    _, problems = effective_policy(ws)
    assert any("dead-grant" in p for p in problems)
