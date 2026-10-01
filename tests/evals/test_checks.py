"""Each kind of check and `score()` (`checks.py`), on synthetic workspaces."""

from __future__ import annotations

from evals.policy_writing.checks import score, snapshot
from tests.evals.workspace import (
    AGENT,
    POLICY,
    by_name,
    make_workspace,
)


def test_decision_checks_pass_and_fail(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    case = {
        "agent": AGENT,
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
        },
    }
    checks = score(case, ws, snapshot(ws), "")
    decisions = [c for c in checks if c.name.startswith("decision:")]
    assert [c.passed for c in decisions] == [True, False, True, True]
    assert "expected allow, got deny" in decisions[1].detail


def test_snapshot_ignores_every_path_under_a_dot_directory(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    skill = ws / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("installed by the harness")
    (ws / ".effective.yaml").write_text("{}")
    assert set(snapshot(ws)) == {"README.md", "policy.yaml"}


def test_no_changes_ignores_an_installed_skill(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    before = snapshot(ws)
    skill = ws / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("installed by the harness")
    checks = by_name(
        score({"agent": AGENT, "expect": {"no_changes": True}}, ws, before, "")
    )
    assert checks["no changes"].passed


def test_no_changes_fails_on_one_changed_file(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    before = snapshot(ws)
    (ws / "README.md").write_text("edited\n")
    checks = by_name(
        score({"agent": AGENT, "expect": {"no_changes": True}}, ws, before, "")
    )
    assert not checks["no changes"].passed
    assert checks["no changes"].detail == "changed: ['README.md']"


def test_changed_and_unchanged(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    before = snapshot(ws)
    (ws / "policy.yaml").write_text(POLICY + "# edited\n")
    case = {
        "agent": AGENT,
        "expect": {"changed": ["policy.yaml"], "unchanged": ["README.md"]},
    }
    checks = by_name(score(case, ws, before, ""))
    assert checks["changed: policy.yaml"].passed
    assert checks["unchanged: README.md"].passed

    case = {
        "agent": AGENT,
        "expect": {"changed": ["README.md"], "unchanged": ["policy.yaml"]},
    }
    checks = by_name(score(case, ws, before, ""))
    assert not checks["changed: README.md"].passed
    assert not checks["unchanged: policy.yaml"].passed


def test_superset_reports_the_violating_probe(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    case = {
        "agent": AGENT,
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
        },
    }
    check = by_name(score(case, ws, snapshot(ws), ""))["superset: support ⊇ billing"]
    assert not check.passed
    assert check.detail == "refund_order: billing=allow, support=approval_required"


def test_when_the_narrower_role_is_missing_then_superset_fails(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    probe = {"tool": "view_orders", "args": {"customer_id": "c1"}}
    case = {
        "agent": AGENT,
        "expect": {
            "superset": [{"wider": "support", "narrower": "nosuch", "probes": [probe]}]
        },
    }
    check = by_name(score(case, ws, snapshot(ws), ""))["superset: support ⊇ nosuch"]
    assert not check.passed
    assert check.detail == "view_orders: nosuch=error, support=allow"


def test_mentions_are_case_insensitive(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    case = {
        "agent": AGENT,
        "expect": {
            "mentions_any": ["Boundary", "ceiling"],
            "mentions_all": ["REFUND", "Security"],
        },
    }
    answer = "The BOUNDARY caps refunds; ask security."
    checks = by_name(score(case, ws, snapshot(ws), answer))
    assert checks["answer mentions one of"].passed
    assert checks["answer mentions all of"].passed

    checks = by_name(score(case, ws, snapshot(ws), "done"))
    assert not checks["answer mentions one of"].passed
    assert checks["answer mentions all of"].detail == "missing ['REFUND', 'Security']"
