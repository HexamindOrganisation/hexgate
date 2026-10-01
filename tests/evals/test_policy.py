"""Loading, validating and dry-running a policy (`policy.py`), on synthetic workspaces."""

from __future__ import annotations

import pytest

from evals.policy_writing.checks import score, snapshot
from evals.policy_writing.policy import decide, effective_policy
from tests.evals.workspace import (
    AGENT,
    by_name,
    make_modules_workspace,
    make_workspace,
)


@pytest.mark.parametrize(
    ("role", "tool", "args", "outcome"),
    [
        ("billing", "refund_order", {"order_id": "o1", "amount": 500}, "allow"),
        ("billing", "refund_order", {"order_id": "o1", "amount": 501}, "deny"),
        (
            "support",
            "refund_order",
            {"order_id": "o1", "amount": 10},
            "approval_required",
        ),
    ],
)
def test_decide_reports_each_outcome(tmp_path, role, tool, args, outcome) -> None:
    ws = make_workspace(tmp_path)
    policy, _ = effective_policy(ws)
    got, raw = decide(policy, role, {"tool": tool, "args": args})
    assert got == outcome, raw


def test_decide_reads_the_outcome_not_the_arguments(tmp_path) -> None:
    # The outcome comes from the verdict, so an argument that spells an outcome
    # can't be mistaken for one.
    ws = make_workspace(tmp_path)
    policy, _ = effective_policy(ws)
    d = {"tool": "refund_order", "args": {"order_id": "ALLOW", "amount": 900}}
    got, reason = decide(policy, "billing", d)
    assert got == "deny"
    assert "amount" in reason


def test_decide_rejects_an_undefined_role(tmp_path) -> None:
    # Not the `default` fallback: a case naming a role the policy lacks fails.
    ws = make_workspace(tmp_path)
    policy, _ = effective_policy(ws)
    got, reason = decide(policy, "suport", {"tool": "view_orders"})
    assert got == "error"
    assert "suport" in reason


def test_clean_policy_is_valid(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    assert by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["valid"].passed


def test_lint_warning_fails_valid(tmp_path) -> None:
    # Parses and builds, but `default` grants a tool no named role grants: a
    # warning at the CLI's default threshold, a failure at ours.
    policy = """\
version: 1
roles:
  default:
    tools:
      refund_order: { mode: allow }
  billing:
    tools:
      view_orders: { mode: allow }
"""
    ws = make_workspace(tmp_path, policy)
    case = {
        "agent": AGENT,
        "expect": {
            "decisions": [{"role": "billing", "tool": "view_orders", "expect": "allow"}]
        },
    }
    checks = score(case, ws, snapshot(ws), "")
    valid = by_name(checks)["valid"]
    assert not valid.passed
    assert "permissive-default" in valid.detail
    # An invalid policy fails its decisions rather than skipping them.
    assert [c.detail for c in checks if c.name.startswith("decision:")] == [
        "policy invalid"
    ]


def test_modules_layout_is_valid(tmp_path) -> None:
    ws = make_modules_workspace(
        tmp_path, "  default: [read_only]\n  billing: [read_only, payments]\n"
    )
    case = {
        "agent": AGENT,
        "expect": {
            "decisions": [
                {
                    "role": "billing",
                    "tool": "refund_order",
                    "args": {"amount": 1001},
                    "expect": "deny",
                }
            ]
        },
    }
    checks = score(case, ws, snapshot(ws), "")
    assert all(c.passed for c in checks), checks


def test_modules_layout_resolves_the_case_agents_column(tmp_path) -> None:
    roles = '  billing:\n    "*": [read_only]\n    shop-bot: [read_only, payments]\n'
    ws = make_modules_workspace(tmp_path, roles)
    refund = {"role": "billing", "tool": "refund_order", "args": {"amount": 5}}
    for agent, outcome in [(AGENT, "allow"), ("ops-bot", "deny")]:
        policy, _ = effective_policy(ws, agent)
        assert decide(policy, "billing", refund)[0] == outcome


def test_modules_layout_fails_valid_on_a_permissive_default(tmp_path) -> None:
    # `policy check` doesn't lint the composed roles; only the resolved policy
    # shows that `default` grants what no named role does.
    ws = make_modules_workspace(
        tmp_path, "  default: [read_only, payments]\n  billing: [read_only]\n"
    )
    valid = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["valid"]
    assert not valid.passed
    assert "permissive-default" in valid.detail


def test_modules_layout_fails_valid_on_a_dead_grant(tmp_path) -> None:
    # A module lint: the boundary denies what a capability grants.
    ws = make_modules_workspace(
        tmp_path, "  default: [read_only]\n  billing: [read_only, payments]\n"
    )
    (ws / "policies" / "boundaries" / "no_views.yaml").write_text(
        "tools:\n  view_orders: { mode: deny }\n"
    )
    valid = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["valid"]
    assert not valid.passed
    assert "dead-grant" in valid.detail


def test_an_empty_policy_is_valid_and_denies(tmp_path) -> None:
    # As `hexgate policy validate` reads it: an empty file is an empty policy.
    ws = make_workspace(tmp_path, "# nothing granted yet\n")
    case = {
        "agent": AGENT,
        "expect": {
            "decisions": [{"role": "default", "tool": "view_orders", "expect": "deny"}]
        },
    }
    checks = score(case, ws, snapshot(ws), "")
    assert all(c.passed for c in checks), checks
