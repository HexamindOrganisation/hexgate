"""Each kind of check and `score()` (`checks.py`), on synthetic workspaces."""

from __future__ import annotations

import datetime

import pytest

from evals.policy_writing.checks import (
    answer_checks,
    decision_checks,
    file_checks,
    score,
    snapshot,
    superset_checks,
)
from evals.policy_writing.policy import effective_policy, policy_columns
from tests.evals.helpers import (
    AGENT,
    PERMISSIVE_DEFAULT,
    by_name,
    make_modules_workspace,
    make_workspace,
)

REFUND = {"tool": "refund_order", "args": {"order_id": "o1", "amount": 5}}
VIEW = {"tool": "view_orders", "args": {"customer_id": "c1"}}


def test_decision_checks_happy_path(tmp_path) -> None:
    policy, _ = effective_policy(make_workspace(tmp_path))
    decisions = [
        {"role": "billing", **REFUND, "expect": "allow"},
        {"roles": ["default", "support"], **VIEW, "expect": ["allow"]},
    ]
    assert all(c.passed for c in decision_checks({"*": policy}, decisions))


def test_when_the_outcome_differs_then_the_decision_check_fails(tmp_path) -> None:
    policy, _ = effective_policy(make_workspace(tmp_path))
    big = {**REFUND, "args": {"order_id": "o1", "amount": 900}}
    [check] = decision_checks(
        {"*": policy}, [{"role": "billing", **big, "expect": "allow"}]
    )
    assert not check.passed
    assert "expected allow, got deny" in check.detail


def test_when_the_role_is_undefined_then_the_decision_check_fails(tmp_path) -> None:
    policy, _ = effective_policy(make_workspace(tmp_path))
    [check] = decision_checks(
        {"*": policy}, [{"role": "suport", **VIEW, "expect": "deny"}]
    )
    assert not check.passed
    assert check.detail.startswith("can't dry-run: role 'suport'")


def test_when_the_policy_is_invalid_then_every_decision_check_fails() -> None:
    [check] = decision_checks({}, [{"role": "billing", **VIEW, "expect": "allow"}])
    assert (check.passed, check.detail) == (False, "policy invalid")


def test_superset_checks_happy_path(tmp_path) -> None:
    policy, _ = effective_policy(make_workspace(tmp_path))
    superset = {"wider": "billing", "narrower": "support", "probes": [VIEW, REFUND]}
    assert superset_checks({"*": policy}, [superset])[0].passed


def test_when_the_wider_role_is_stricter_then_superset_reports_the_probe(
    tmp_path,
) -> None:
    policy, _ = effective_policy(make_workspace(tmp_path))
    superset = {"wider": "support", "narrower": "billing", "probes": [VIEW, REFUND]}
    [check] = superset_checks({"*": policy}, [superset])
    assert not check.passed
    assert check.detail == "refund_order: billing=allow, support=approval_required"


def test_when_a_role_is_missing_then_superset_fails(tmp_path) -> None:
    policy, _ = effective_policy(make_workspace(tmp_path))
    superset = {"wider": "support", "narrower": "nosuch", "probes": [VIEW]}
    [check] = superset_checks({"*": policy}, [superset])
    assert not check.passed
    assert check.detail.startswith("view_orders: can't dry-run: role 'nosuch'")


def test_snapshot_happy_path(tmp_path) -> None:
    # Dot paths are tooling: the harness installs the skill under .claude/.
    ws = make_workspace(tmp_path)
    skill = ws / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("installed by the harness")
    (ws / ".effective.yaml").write_text("{}")
    assert set(snapshot(ws)) == {"agents.json", "audit.json", "policy.yaml"}


def test_file_checks_happy_path() -> None:
    before = {"policy.yaml": "a", "README.md": "r"}
    after = {"policy.yaml": "b", "README.md": "r"}
    expect = {"changed": ["policy.yaml"], "unchanged": ["README.md"]}
    assert all(c.passed for c in file_checks(expect, before, after))


def test_when_files_go_the_other_way_then_file_checks_fail() -> None:
    before = {"policy.yaml": "a", "README.md": "r"}
    after = {"policy.yaml": "b", "README.md": "r"}
    expect = {"changed": ["README.md"], "unchanged": ["policy.yaml"]}
    assert not any(c.passed for c in file_checks(expect, before, after))


def test_when_one_file_changes_then_no_changes_fails() -> None:
    before = {"policy.yaml": "a", "README.md": "r"}
    [check] = file_checks({"no_changes": True}, before, {**before, "README.md": "x"})
    assert (check.passed, check.detail) == (False, "changed: ['README.md']")


def test_when_an_unchanged_path_does_not_exist_then_it_fails() -> None:
    files = {"policy.yaml": "a"}
    [check] = file_checks({"unchanged": ["policies/boundary/org.yaml"]}, files, files)
    assert (check.passed, check.detail) == (False, "no such file")


def test_answer_checks_happy_path() -> None:
    expect = {"mentions_any": ["Boundary", "ceiling"], "mentions_all": ["REFUND"]}
    checks = answer_checks(expect, "The BOUNDARY caps refunds.")
    assert all(c.passed for c in checks)


def test_when_the_answer_misses_the_words_then_answer_checks_fail() -> None:
    expect = {"mentions_any": ["boundary"], "mentions_all": ["refund", "security"]}
    checks = by_name(answer_checks(expect, "done"))
    assert not checks["answer mentions one of"].passed
    assert checks["answer mentions all of"].detail == "missing ['refund', 'security']"


def test_when_a_call_holds_a_yaml_date_then_its_check_is_named(tmp_path) -> None:
    policy, _ = effective_policy(make_workspace(tmp_path))
    call = {"role": "default", **VIEW, "args": {"since": datetime.date(2026, 1, 1)}}
    [check] = decision_checks({"*": policy}, [{**call, "expect": "allow"}])
    assert "2026-01-01" in check.name


def test_score_happy_path(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    before = snapshot(ws)
    # Installing the skill is not an edit.
    skill = ws / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("installed by the harness")
    case = {
        "agent": AGENT,
        "expect": {
            "decisions": [{"role": "billing", **REFUND, "expect": "allow"}],
            "no_changes": True,
            "mentions_any": ["refund"],
        },
    }
    checks = score(case, ws, before, "Billing can refund.")
    assert [c.name for c in checks if c.passed] == [c.name for c in checks]


def test_when_the_policy_is_invalid_then_score_fails_valid_and_decisions(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path, PERMISSIVE_DEFAULT)
    case = {
        "agent": AGENT,
        "expect": {"decisions": [{"role": "billing", **VIEW, "expect": "allow"}]},
    }
    checks = score(case, ws, snapshot(ws), "")
    assert "permissive-default" in by_name(checks)["valid"].detail
    assert [c.passed for c in checks] == [False, False]


# A role-wide case on a module tree: every roles.yaml column


@pytest.mark.parametrize(
    ("shop_bot_cell", "passed"),
    [("[read_only]", False), ("[read_only, payments]", True)],
)
def test_a_role_wide_decision_must_hold_on_every_agents_column(
    tmp_path, shop_bot_cell, passed
) -> None:
    # Refunds granted on "*"; shop-bot's own cell replaces "*" for it.
    roles = (
        f'  support:\n    "*": [read_only, payments]\n    shop-bot: {shop_bot_cell}\n'
    )
    ws = make_modules_workspace(tmp_path, roles)
    columns, _ = policy_columns(ws, None, effective_policy(ws)[0])
    [check] = decision_checks(
        columns, [{"role": "support", **REFUND, "expect": "allow"}]
    )
    assert check.passed is passed
    if not passed:
        assert check.detail.endswith("(column shop-bot)")


def test_a_role_wide_superset_must_hold_on_every_agents_column(tmp_path) -> None:
    # On "*" support may refund like billing; shop-bot's own cell narrows it.
    roles = (
        '  support:\n    "*": [read_only, payments]\n    shop-bot: [read_only]\n'
        "  billing: [read_only, payments]\n"
    )
    ws = make_modules_workspace(tmp_path, roles)
    columns, _ = policy_columns(ws, None, effective_policy(ws)[0])
    superset = {"narrower": "billing", "wider": "support", "probes": [REFUND]}
    [check] = superset_checks(columns, [superset])
    assert check.detail == "refund_order: billing=allow, support=deny (column shop-bot)"


def test_when_an_agents_column_is_invalid_then_score_fails_valid(tmp_path) -> None:
    roles = '  default:\n    "*": [read_only]\n    ops-bot: [read_only, payments]\n'
    ws = make_modules_workspace(tmp_path, roles + "  billing: [read_only]\n")
    case = {"expect": {"decisions": [{"role": "billing", **VIEW, "expect": "allow"}]}}
    checks = by_name(score(case, ws, snapshot(ws), ""))
    assert checks["valid"].detail.startswith("column ops-bot:")
    # The invalid column's dry-runs can't run, so none passes by being skipped.
    decision = next(c for n, c in checks.items() if n.startswith("decision:"))
    assert (decision.passed, decision.detail) == (False, "policy invalid")
