"""Each kind of check and `score()` (`checks.py`), on synthetic workspaces."""

from __future__ import annotations

import datetime
import json

import pytest

from evals.policy_writing.checks import (
    NAME_CHECKS,
    answer_checks,
    decision_checks,
    file_checks,
    name_checks,
    score,
    snapshot,
    superset_checks,
)
from tests.evals.helpers import (
    AGENT,
    PERMISSIVE_DEFAULT,
    POLICY,
    by_name,
    install_skill,
    make_modules_workspace,
    make_workspace,
    manifest_tool,
    valid_policy,
)

# The fixture's org boundary, open for more tools.
BOUNDARY = (
    "default_policy: { mode: allow }\n"
    "tools:\n"
    '  refund_order: { mode: allow, constraints: ["args.amount <= 1000"] }\n'
)
REFUND = {"tool": "refund_order", "args": {"order_id": "o1", "amount": 5}}
VIEW = {"tool": "view_orders", "args": {"customer_id": "c1"}}


def test_decision_checks_happy_path(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    decisions = [
        {"role": "billing", **REFUND, "expect": "allow"},
        {"roles": ["default", "support"], **VIEW, "expect": ["allow"]},
    ]
    assert all(c.passed for c in decision_checks(policy, decisions))


def test_when_the_outcome_differs_then_the_decision_check_fails(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    big = {**REFUND, "args": {"order_id": "o1", "amount": 900}}
    [check] = decision_checks(policy, [{"role": "billing", **big, "expect": "allow"}])
    assert not check.passed
    assert "expected allow, got deny" in check.detail


def test_when_the_role_is_undefined_then_the_decision_check_fails(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    [check] = decision_checks(policy, [{"role": "suport", **VIEW, "expect": "deny"}])
    assert not check.passed
    assert check.detail.startswith("can't dry-run: role 'suport'")


def test_when_the_policy_is_invalid_then_every_decision_check_fails() -> None:
    [check] = decision_checks(None, [{"role": "billing", **VIEW, "expect": "allow"}])
    assert (check.passed, check.detail) == (False, "policy invalid")


def test_superset_checks_happy_path(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    superset = {"wider": "billing", "narrower": "support", "probes": [VIEW, REFUND]}
    assert superset_checks(policy, [superset])[0].passed


def test_when_the_wider_role_is_stricter_then_superset_reports_the_probe(
    tmp_path,
) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    superset = {"wider": "support", "narrower": "billing", "probes": [VIEW, REFUND]}
    [check] = superset_checks(policy, [superset])
    assert not check.passed
    assert check.detail == "refund_order: billing=allow, support=approval_required"


def test_when_a_role_is_missing_then_superset_fails(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    superset = {"wider": "support", "narrower": "nosuch", "probes": [VIEW]}
    [check] = superset_checks(policy, [superset])
    assert not check.passed
    assert check.detail.startswith("view_orders: can't dry-run: role 'nosuch'")


def test_name_checks_happy_path(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    policy = valid_policy(ws, AGENT)
    before = snapshot(ws)
    checks = name_checks(policy, ws, before, before)
    assert [(c.name, c.passed, c.detail) for c in checks] == [
        (n, True, "") for n in NAME_CHECKS
    ]


@pytest.mark.parametrize(
    ("module", "text"),
    [
        # An org-wide deny on ops-bot's tool and skill: linking keeps a deny.
        (
            "boundaries/org.yaml",
            BOUNDARY
            + "  wire_transfer: { mode: deny }\nskills:\n  ledger: { mode: deny }\n",
        ),
        # A `"*"` cell's capability may grant any agent's tool.
        (
            "capabilities/payments.yaml",
            "tools:\n  refund_order: { mode: allow }\n  wire_transfer: { mode: allow }\n",
        ),
    ],
)
def test_when_a_module_tree_names_another_agents_tool_then_name_checks_accept_it(
    tmp_path, module, text
) -> None:
    ws = make_modules_workspace(tmp_path)
    (ws / "policies" / module).write_text(text)
    policy = valid_policy(ws, AGENT, modules=True)
    before = snapshot(ws)
    checks = name_checks(policy, ws, before, before, modules=True)
    assert [(c.passed, c.detail) for c in checks] == [(True, "")] * 2


def test_when_a_module_tree_denies_an_invented_tool_then_name_checks_flag_it(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path)
    (ws / "policies" / "boundaries" / "org.yaml").write_text(
        BOUNDARY + "  wire_transfr: { mode: deny }\n"
    )
    policy = valid_policy(ws, AGENT, modules=True)
    before = snapshot(ws)
    checks = name_checks(policy, ws, before, before, modules=True)
    assert (checks[0].passed, checks[0].detail) == (
        False,
        "not in any agent's manifest: ['wire_transfr']",
    )


def test_when_the_policy_invents_names_then_both_name_checks_fail(tmp_path) -> None:
    policy = (
        POLICY
        + '      wire_transfer: { mode: allow, constraints: ["ctx.tier == 1"] }\n'
    )
    ws = make_workspace(tmp_path, policy)
    policy = valid_policy(ws, AGENT)
    before = snapshot(ws)
    checks = [(c.passed, c.detail) for c in name_checks(policy, ws, before, before)]
    assert checks == [
        (False, "not in shop-bot's manifest: ['wire_transfer']"),
        (False, "not in the manifest or audit.json: ['wire_transfer: ctx.tier']"),
    ]


def test_when_a_name_source_is_unreadable_then_both_name_checks_fail(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    policy = valid_policy(ws, AGENT)
    (ws / "agents.json").write_text("{not json")
    after = snapshot(ws)
    checks = name_checks(policy, ws, after, after)
    assert [(c.name, c.passed) for c in checks] == [(n, False) for n in NAME_CHECKS]
    assert all(
        c.detail.startswith("agents.json / audit.json unreadable: ") for c in checks
    )


@pytest.mark.parametrize("edited", ["agents.json", "audit.json"])
def test_when_the_agent_edits_a_name_source_then_both_name_checks_fail(
    tmp_path, edited
) -> None:
    ws = make_workspace(tmp_path)
    policy = valid_policy(ws, AGENT)
    before = snapshot(ws)
    # E.g. the agent "fixes" an invented name by adding it to the manifest.
    (ws / edited).write_text("[]")
    checks = name_checks(policy, ws, before, snapshot(ws))
    assert [(c.passed, c.detail) for c in checks] == [
        (False, f"edited during the run: ['{edited}']")
    ] * 2


def test_snapshot_happy_path(tmp_path) -> None:
    # Dot paths are tooling: the harness installs the skill under .claude/.
    ws = make_workspace(tmp_path)
    install_skill(ws)
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


@pytest.mark.parametrize("kind", ["changed", "unchanged"])
def test_when_a_listed_path_does_not_exist_then_it_fails(kind) -> None:
    # A path in neither snapshot is a typo in the case.
    files = {"policy.yaml": "a"}
    [check] = file_checks({kind: ["policy.yml"]}, files, files)
    assert (check.passed, check.detail) == (False, "no such file")


def test_when_two_decisions_differ_only_in_expect_then_their_checks_are_named_apart(
    tmp_path,
) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    call = {"role": "billing", **REFUND}
    checks = decision_checks(
        policy, [{**call, "expect": "allow"}, {**call, "expect": "deny"}]
    )
    assert len(by_name(checks)) == 2


def test_answer_checks_happy_path() -> None:
    expect = {"mentions_any": ["Boundary", "ceiling"], "mentions_all": ["REFUND"]}
    checks = answer_checks(expect, "The BOUNDARY caps a refund.")
    assert all(c.passed for c in checks)


def test_when_the_answer_misses_the_words_then_answer_checks_fail() -> None:
    expect = {"mentions_any": ["boundary"], "mentions_all": ["refund", "security"]}
    checks = by_name(answer_checks(expect, "done"))
    assert not checks["answer mentions one of"].passed
    assert checks["answer mentions all of"].detail == "missing ['refund', 'security']"


def test_when_a_call_holds_a_yaml_date_then_its_check_is_named(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    call = {"role": "default", **VIEW, "args": {"since": datetime.date(2026, 1, 1)}}
    [check] = decision_checks(policy, [{**call, "expect": "allow"}])
    assert "2026-01-01" in check.name


def test_when_a_word_appears_only_inside_another_then_it_is_not_mentioned() -> None:
    [check] = answer_checks({"mentions_any": ["no"]}, "I know it is fine")
    assert not check.passed


@pytest.mark.parametrize("answer", ["the cap is 1,500", "the cap is 500.5"])
def test_when_a_number_is_part_of_a_bigger_one_then_it_is_not_mentioned(
    answer,
) -> None:
    [check] = answer_checks({"mentions_any": ["500"]}, answer)
    assert not check.passed


def test_when_a_number_ends_a_sentence_then_it_is_mentioned() -> None:
    [check] = answer_checks({"mentions_any": ["500"]}, "The cap is 500.")
    assert check.passed


def test_when_a_word_is_part_of_a_snake_case_name_then_it_is_mentioned() -> None:
    [check] = answer_checks({"mentions_any": ["approval"]}, "set to approval_required")
    assert check.passed


def test_when_the_workspace_has_a_broken_symlink_then_snapshot_skips_it(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path)
    (ws / "dangling").symlink_to(ws / "missing")
    assert "dangling" not in snapshot(ws)


def test_score_happy_path(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    before = snapshot(ws)
    # Installing the skill is not an edit.
    install_skill(ws)
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
    assert set(NAME_CHECKS) <= {c.name for c in checks}


def test_when_a_module_tree_denies_another_agents_tool_then_score_passes(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path)
    (ws / "policies" / "boundaries" / "org.yaml").write_text(
        BOUNDARY + "  wire_transfer: { mode: deny }\n"
    )
    checks = score({"agent": AGENT}, ws, snapshot(ws), "")
    assert [c.name for c in checks if not c.passed] == []


def test_when_the_agent_edits_agents_json_then_score_fails_both_name_checks(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path, POLICY + "      wire_transfer: { mode: allow }\n")
    before = snapshot(ws)
    # The agent "fixes" its invented tool by adding it to the manifest.
    agents = json.loads((ws / "agents.json").read_text())
    agents[0]["manifest"]["tools"].append(manifest_tool("wire_transfer", iban="s"))
    (ws / "agents.json").write_text(json.dumps(agents))
    checks = by_name(score({"agent": AGENT}, ws, before, ""))
    for name in NAME_CHECKS:
        assert (checks[name].passed, checks[name].detail) == (
            False,
            "edited during the run: ['agents.json']",
        )


def test_when_a_case_expects_a_superset_then_score_runs_it(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    superset = {"wider": "support", "narrower": "billing", "probes": [REFUND]}
    case = {"agent": AGENT, "expect": {"superset": [superset]}}
    check = by_name(score(case, ws, snapshot(ws), ""))["superset 1: support ⊇ billing"]
    assert (check.passed, check.detail) == (
        False,
        "refund_order: billing=allow, support=approval_required",
    )


def test_when_the_agent_adds_a_policies_dir_then_score_still_reads_policy_yaml(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path)
    before = snapshot(ws)
    (ws / "policies").mkdir()
    (ws / "policies" / "draft.md").write_text("notes")
    case = {"agent": AGENT, "expect": {}}
    assert by_name(score(case, ws, before, ""))["valid"].passed


def test_when_the_starting_project_is_a_module_tree_then_score_resolves_it(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path)
    refund = {"role": "billing", "tool": "refund_order", "args": {"amount": 1001}}
    case = {"agent": AGENT, "expect": {"decisions": [{**refund, "expect": "deny"}]}}
    assert all(c.passed for c in score(case, ws, snapshot(ws), ""))


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
