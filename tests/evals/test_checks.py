"""The policy-writing scorer, on small synthetic workspaces (no LLM, no case data)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.policy_writing.checks import (
    decide,
    effective_policy,
    read_manifest,
    score,
    snapshot,
    unknown_refs,
)

TOOLS_MD = """\
# shop-bot: tools and caller attributes

## Tools

| Tool | Arguments |
|---|---|
| `view_orders` | `customer_id: string` |
| `refund_order` | `order_id: string`, `amount: number` |

## Caller attributes (`ctx.*`)

| Attribute | Type | Example |
|---|---|---|
| `department` | string | `"finance"` |
"""

POLICY = """\
version: 1
roles:
  default:
    tools:
      view_orders: { mode: allow }
  support:
    tools:
      view_orders: { mode: allow }
      refund_order:
        mode: approval_required
  billing:
    tools:
      view_orders: { mode: allow }
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 500
"""


def _workspace(tmp_path: Path, policy: str = POLICY) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "TOOLS.md").write_text(TOOLS_MD)
    (ws / "policy.yaml").write_text(policy)
    return ws


def _by_name(checks) -> dict:
    return {c.name: c for c in checks}


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
    ws = _workspace(tmp_path)
    policy, _ = effective_policy(ws)
    got, raw = decide(policy, role, {"tool": tool, "args": args})
    assert got == outcome, raw


def test_decide_reads_the_outcome_not_the_arguments(tmp_path) -> None:
    # The outcome comes from the verdict, so an argument that spells an outcome
    # can't be mistaken for one.
    ws = _workspace(tmp_path)
    policy, _ = effective_policy(ws)
    d = {"tool": "refund_order", "args": {"order_id": "ALLOW", "amount": 900}}
    got, reason = decide(policy, "billing", d)
    assert got == "deny"
    assert "amount" in reason


def test_decide_rejects_an_undefined_role(tmp_path) -> None:
    # Not the `default` fallback: a case naming a role the policy lacks fails.
    ws = _workspace(tmp_path)
    policy, _ = effective_policy(ws)
    got, reason = decide(policy, "suport", {"tool": "view_orders"})
    assert got == "error"
    assert "suport" in reason


def test_decision_checks_pass_and_fail(tmp_path) -> None:
    ws = _workspace(tmp_path)
    case = {
        "expect": {
            "decisions": [
                {
                    "role": "billing",
                    "tool": "refund_order",
                    "args": {"amount": 1},
                    "expect": "allow",
                },
                {
                    "role": "billing",
                    "tool": "refund_order",
                    "args": {"amount": 900},
                    "expect": "allow",
                },
                {
                    "roles": ["default", "support"],
                    "tool": "view_orders",
                    "expect": ["allow"],
                },
            ]
        }
    }
    checks = score(case, ws, snapshot(ws), "")
    decisions = [c for c in checks if c.name.startswith("decision:")]
    assert [c.passed for c in decisions] == [True, False, True, True]
    assert "expected allow, got deny" in decisions[1].detail


def test_unknown_refs_flags_an_argument_missing_from_tools_md(tmp_path) -> None:
    policy = POLICY.replace(
        "- args.amount <= 500", '- args.tier != "gold" or args.amount <= 1000'
    )
    ws = _workspace(tmp_path, policy)
    tools, attrs = read_manifest(ws)
    policy, _ = effective_policy(ws)
    assert unknown_refs(policy.payload, tools, attrs) == ["refund_order: args.tier"]


def test_unknown_refs_accepts_a_known_caller_attribute(tmp_path) -> None:
    policy = POLICY.replace("- args.amount <= 500", '- ctx.department == "finance"')
    ws = _workspace(tmp_path, policy)
    tools, attrs = read_manifest(ws)
    policy, _ = effective_policy(ws)
    assert unknown_refs(policy.payload, tools, attrs) == []


def test_snapshot_ignores_every_path_under_a_dot_directory(tmp_path) -> None:
    ws = _workspace(tmp_path)
    skill = ws / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("installed by the harness")
    (ws / ".effective.yaml").write_text("{}")
    assert set(snapshot(ws)) == {"TOOLS.md", "policy.yaml"}


def test_no_changes_ignores_an_installed_skill(tmp_path) -> None:
    ws = _workspace(tmp_path)
    before = snapshot(ws)
    skill = ws / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("installed by the harness")
    checks = _by_name(score({"expect": {"no_changes": True}}, ws, before, ""))
    assert checks["no changes"].passed


def test_no_changes_fails_on_one_changed_file(tmp_path) -> None:
    ws = _workspace(tmp_path)
    before = snapshot(ws)
    (ws / "TOOLS.md").write_text(TOOLS_MD + "\nedited\n")
    checks = _by_name(score({"expect": {"no_changes": True}}, ws, before, ""))
    assert not checks["no changes"].passed
    assert checks["no changes"].detail == "changed: ['TOOLS.md']"


def test_changed_and_unchanged(tmp_path) -> None:
    ws = _workspace(tmp_path)
    before = snapshot(ws)
    (ws / "policy.yaml").write_text(POLICY + "# edited\n")
    case = {"expect": {"changed": ["policy.yaml"], "unchanged": ["TOOLS.md"]}}
    checks = _by_name(score(case, ws, before, ""))
    assert checks["changed: policy.yaml"].passed
    assert checks["unchanged: TOOLS.md"].passed

    case = {"expect": {"changed": ["TOOLS.md"], "unchanged": ["policy.yaml"]}}
    checks = _by_name(score(case, ws, before, ""))
    assert not checks["changed: TOOLS.md"].passed
    assert not checks["unchanged: policy.yaml"].passed


def test_superset_reports_the_violating_probe(tmp_path) -> None:
    ws = _workspace(tmp_path)
    case = {
        "expect": {
            "superset": [
                {
                    "wider": "support",
                    "narrower": "billing",
                    "probes": [
                        {"tool": "view_orders", "args": {"customer_id": "c1"}},
                        {
                            "tool": "refund_order",
                            "args": {"order_id": "o1", "amount": 5},
                        },
                    ],
                }
            ]
        }
    }
    check = _by_name(score(case, ws, snapshot(ws), ""))["superset: support ⊇ billing"]
    assert not check.passed
    assert check.detail == "refund_order: billing=allow, support=approval_required"


def test_mentions_are_case_insensitive(tmp_path) -> None:
    ws = _workspace(tmp_path)
    case = {
        "expect": {
            "mentions_any": ["Boundary", "ceiling"],
            "mentions_all": ["REFUND", "Security"],
        }
    }
    answer = "The BOUNDARY caps refunds; ask security."
    checks = _by_name(score(case, ws, snapshot(ws), answer))
    assert checks["answer mentions one of"].passed
    assert checks["answer mentions all of"].passed

    checks = _by_name(score(case, ws, snapshot(ws), "done"))
    assert not checks["answer mentions one of"].passed
    assert checks["answer mentions all of"].detail == "missing ['REFUND', 'Security']"


def test_unknown_tool_fails(tmp_path) -> None:
    policy = POLICY + "      wire_transfer: { mode: allow }\n"
    ws = _workspace(tmp_path, policy)
    check = _by_name(score({}, ws, snapshot(ws), ""))["only known tools"]
    assert not check.passed
    assert check.detail == "not in TOOLS.md: ['wire_transfer']"


def test_clean_policy_is_valid(tmp_path) -> None:
    ws = _workspace(tmp_path)
    assert _by_name(score({}, ws, snapshot(ws), ""))["valid"].passed


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
    ws = _workspace(tmp_path, policy)
    case = {
        "expect": {
            "decisions": [{"role": "billing", "tool": "view_orders", "expect": "allow"}]
        }
    }
    checks = score(case, ws, snapshot(ws), "")
    valid = _by_name(checks)["valid"]
    assert not valid.passed
    assert "permissive-default" in valid.detail
    # An invalid policy fails its decisions rather than skipping them.
    assert [c.detail for c in checks if c.name.startswith("decision:")] == [
        "policy invalid"
    ]


def _modules_workspace(tmp_path: Path, roles: str) -> Path:
    ws = tmp_path / "ws"
    (ws / "policies" / "boundaries").mkdir(parents=True)
    (ws / "policies" / "capabilities").mkdir()
    (ws / "TOOLS.md").write_text(TOOLS_MD)
    (ws / "policies" / "boundaries" / "org.yaml").write_text(
        "default_policy: { mode: allow }\n"
        "tools:\n"
        '  refund_order: { mode: allow, constraints: ["args.amount <= 1000"] }\n'
    )
    (ws / "policies" / "capabilities" / "read_only.yaml").write_text(
        "tools:\n  view_orders: { mode: allow }\n"
    )
    (ws / "policies" / "capabilities" / "payments.yaml").write_text(
        "tools:\n  refund_order: { mode: allow }\n"
    )
    (ws / "roles.yaml").write_text(f"version: 1\nroles:\n{roles}")
    return ws


def test_modules_layout_is_valid(tmp_path) -> None:
    ws = _modules_workspace(
        tmp_path, "  default: [read_only]\n  billing: [read_only, payments]\n"
    )
    case = {
        "expect": {
            "decisions": [
                {
                    "role": "billing",
                    "tool": "refund_order",
                    "args": {"amount": 1001},
                    "expect": "deny",
                }
            ]
        }
    }
    checks = score(case, ws, snapshot(ws), "")
    assert all(c.passed for c in checks), checks


def test_modules_layout_fails_valid_on_a_permissive_default(tmp_path) -> None:
    # `policy check` doesn't lint the composed roles; only the resolved policy
    # shows that `default` grants what no named role does.
    ws = _modules_workspace(
        tmp_path, "  default: [read_only, payments]\n  billing: [read_only]\n"
    )
    valid = _by_name(score({}, ws, snapshot(ws), ""))["valid"]
    assert not valid.passed
    assert "permissive-default" in valid.detail


def test_modules_layout_fails_valid_on_a_dead_grant(tmp_path) -> None:
    # A module lint: the boundary denies what a capability grants.
    ws = _modules_workspace(
        tmp_path, "  default: [read_only]\n  billing: [read_only, payments]\n"
    )
    (ws / "policies" / "boundaries" / "no_views.yaml").write_text(
        "tools:\n  view_orders: { mode: deny }\n"
    )
    valid = _by_name(score({}, ws, snapshot(ws), ""))["valid"]
    assert not valid.passed
    assert "dead-grant" in valid.detail


def test_an_empty_policy_is_valid_and_denies(tmp_path) -> None:
    # As `hexgate policy validate` reads it: an empty file is an empty policy.
    ws = _workspace(tmp_path, "# nothing granted yet\n")
    case = {
        "expect": {
            "decisions": [{"role": "default", "tool": "view_orders", "expect": "deny"}]
        }
    }
    checks = score(case, ws, snapshot(ws), "")
    assert all(c.passed for c in checks), checks
