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
    OPS_COLUMN_PERMISSIVE_DEFAULT,
    PERMISSIVE_DEFAULT,
    POLICY,
    by_name,
    install_skill,
    make_modules_workspace,
    make_workspace,
    manifest_tool,
    role_wide_columns,
    valid_policy,
)

REFUND = {"tool": "refund_order", "args": {"order_id": "o1", "amount": 5}}
VIEW = {"tool": "view_orders", "args": {"customer_id": "c1"}}


def test_decision_checks_happy_path(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    decisions = [
        {"role": "billing", **REFUND, "expect": "allow"},
        {"roles": ["default", "support"], **VIEW, "expect": ["allow"]},
    ]
    assert all(c.passed for c in decision_checks({"*": policy}, decisions))


def test_when_the_outcome_differs_then_the_decision_check_fails(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    big = {**REFUND, "args": {"order_id": "o1", "amount": 900}}
    [check] = decision_checks(
        {"*": policy}, [{"role": "billing", **big, "expect": "allow"}]
    )
    assert not check.passed
    assert "expected allow, got deny" in check.detail


def test_when_the_role_is_undefined_then_the_decision_check_fails(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    [check] = decision_checks(
        {"*": policy}, [{"role": "suport", **VIEW, "expect": "deny"}]
    )
    assert not check.passed
    assert check.detail.startswith("can't dry-run: role 'suport'")


def test_when_the_policy_is_invalid_then_every_decision_check_fails() -> None:
    [check] = decision_checks({}, [{"role": "billing", **VIEW, "expect": "allow"}])
    assert (check.passed, check.detail) == (False, "policy invalid")


def test_superset_checks_happy_path(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    superset = {"wider": "billing", "narrower": "support", "probes": [VIEW, REFUND]}
    assert superset_checks({"*": policy}, [superset])[0].passed


def test_when_the_wider_role_is_stricter_then_superset_reports_the_probe(
    tmp_path,
) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    superset = {"wider": "support", "narrower": "billing", "probes": [VIEW, REFUND]}
    [check] = superset_checks({"*": policy}, [superset])
    assert not check.passed
    assert check.detail == "refund_order: billing=allow, support=approval_required"


def test_when_a_role_is_missing_then_superset_fails(tmp_path) -> None:
    policy = valid_policy(make_workspace(tmp_path))
    superset = {"wider": "support", "narrower": "nosuch", "probes": [VIEW]}
    [check] = superset_checks({"*": policy}, [superset])
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


# The two name checks, in NAME_CHECKS order: which one an invented name fails.
KEYS, ARGS = (False, True), (True, False)


@pytest.mark.parametrize(
    ("rule", "fails"),
    [
        ("      refnd_order: { mode: allow }\n", KEYS),
        ("      refnd_order: { mode: deny }\n", KEYS),  # graded info by the SDK
        (
            '      view_orders: { mode: allow, constraints: ["args.customr == 1"] }\n',
            ARGS,
        ),
        ('      view_orders: { mode: allow, constraints: ["ctx.tier == 1"] }\n', ARGS),
        ("      wire_transfer: { mode: allow }\n", KEYS),  # only ops-bot's
    ],
)
def test_when_a_policy_file_invents_a_name_then_one_name_check_fails(
    tmp_path, rule, fails
) -> None:
    ws = make_workspace(tmp_path, POLICY + rule)
    checks = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))
    assert checks["valid"].passed
    assert tuple(checks[n].passed for n in NAME_CHECKS) == fails


@pytest.mark.parametrize(
    "role",
    [
        {"skills": {"pdff": {"mode": "allow"}}},
        {"guards": {"redact_pi": {"enabled": True}}},
    ],
)
def test_when_a_policy_file_invents_a_skill_or_guard_then_the_keys_check_fails(
    tmp_path, role
) -> None:
    # One role, so its guards can't diverge from another's.
    doc = {
        "version": 1,
        "roles": {"default": {"tools": {"view_orders": {"mode": "allow"}}, **role}},
    }
    ws = make_workspace(tmp_path, json.dumps(doc))
    checks = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))
    assert checks["valid"].passed, checks["valid"].detail
    assert tuple(checks[n].passed for n in NAME_CHECKS) == KEYS


# shop-bot's own column, which `check_project` checks against its manifest.
OWN_COLUMN = (
    "  default: [read_only]\n"
    "  billing:\n    '*': [read_only, payments]\n    shop-bot: [read_only, inv]\n"
)


@pytest.mark.parametrize(
    ("capability", "fails"),
    [
        ("tools:\n  refnd_order: { mode: allow }\n", KEYS),
        ("skills:\n  pdff: { mode: allow, via: [resource] }\n", KEYS),
        (
            'tools:\n  refund_order: { mode: allow, constraints: ["args.amout < 5"] }\n',
            ARGS,
        ),
        (
            'tools:\n  refund_order: { mode: allow, constraints: ["ctx.tier == 1"] }\n',
            ARGS,
        ),
    ],
)
def test_when_a_module_tree_invents_a_name_then_one_name_check_fails(
    tmp_path, capability, fails
) -> None:
    ws = make_modules_workspace(tmp_path, OWN_COLUMN)
    (ws / "policies" / "capabilities" / "inv.yaml").write_text(capability)
    checks = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))
    assert checks["valid"].passed, checks["valid"].detail
    assert tuple(checks[n].passed for n in NAME_CHECKS) == fails


@pytest.mark.parametrize(
    ("agent", "source", "broken", "detail"),
    [
        (
            AGENT,
            "agents.json",
            "{not json",
            "agents.json has no readable manifest for 'shop-bot'",
        ),
        (
            "draft-bot",
            None,
            None,
            "agents.json has no readable manifest for 'draft-bot'",
        ),
        (AGENT, "audit.json", "null", "audit.json unreadable: "),
    ],
)
def test_when_a_name_source_is_unreadable_then_both_name_checks_fail(
    tmp_path, agent, source, broken, detail
) -> None:
    ws = make_workspace(tmp_path)
    if source:
        (ws / source).write_text(broken)
    policy = valid_policy(ws, agent)
    after = snapshot(ws)
    checks = name_checks(policy, ws, after, after)
    assert [(c.name, c.passed) for c in checks] == [(n, False) for n in NAME_CHECKS]
    assert all(c.detail.startswith(detail) for c in checks)


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
        {"*": policy}, [{**call, "expect": "allow"}, {**call, "expect": "deny"}]
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
    [check] = decision_checks({"*": policy}, [{**call, "expect": "allow"}])
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


def test_when_the_agent_edits_agents_json_then_score_fails_both_name_checks(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path, POLICY + "      wire_transfer: { mode: allow }\n")
    before = snapshot(ws)
    # The agent "fixes" its invented tool by adding it to the manifest, which
    # the drift then reads.
    agents = json.loads((ws / "agents.json").read_text())
    agents[0]["manifest"]["tools"].append(manifest_tool("wire_transfer", iban="s"))
    (ws / "agents.json").write_text(json.dumps(agents))
    checks = by_name(score({"agent": AGENT}, ws, before, ""))
    assert [(checks[n].passed, checks[n].detail) for n in NAME_CHECKS] == [
        (False, "edited during the run: ['agents.json']")
    ] * 2


def test_when_a_module_mistake_is_in_several_cells_then_its_name_check_lists_it_once(
    tmp_path,
) -> None:
    roles = "  default: [read_only, payments]\n  billing: [read_only, payments]\n"
    ws = make_modules_workspace(tmp_path, roles)
    (ws / "policies" / "capabilities" / "payments.yaml").write_text(
        "tools:\n  refnd_order: { mode: allow }\n"
    )
    checks = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))
    assert checks[NAME_CHECKS[0]].detail.count("refnd_order") == 1


def test_when_another_agents_column_invents_a_tool_then_the_detail_names_that_agent(
    tmp_path,
) -> None:
    roles = "  default: [read_only]\n  billing:\n    '*': [read_only, payments]\n    ops-bot: [wire]\n"
    ws = make_modules_workspace(tmp_path, roles)
    (ws / "policies" / "capabilities" / "wire.yaml").write_text(
        "tools:\n  wire_transfr: { mode: allow }\n"
    )
    checks = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))
    assert checks[NAME_CHECKS[0]].detail.startswith("[unknown-tool] [agent ops-bot] ")


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


# A role or project-wide case (no agent) on a module tree: every roles.yaml column


# Refunds on "*" but not in shop-bot's own cell, which replaces "*" for it.
SPLIT = '  support:\n    "*": [read_only, payments]\n    shop-bot: [read_only]\n'


@pytest.mark.parametrize("expect", ["allow", ["allow", "approval_required"]])
@pytest.mark.parametrize(
    ("roles", "passed"),
    [
        (SPLIT, True),  # "*" allows it: some agent can refund
        ('  support:\n    "*": [read_only]\n    shop-bot: [read_only]\n', False),
    ],
)
def test_when_the_case_names_no_agent_then_an_allow_needs_one_column(
    tmp_path, roles, passed, expect
) -> None:
    ws = make_modules_workspace(tmp_path, roles)
    allow = {"role": "support", **REFUND, "expect": expect}
    [check] = decision_checks(role_wide_columns(ws), [allow])
    assert check.passed is passed
    if passed:
        assert check.detail == ""
    else:
        assert "(column *)" in check.detail and "(column shop-bot)" in check.detail


@pytest.mark.parametrize(
    "expect", ["deny", "approval_required", ["deny", "approval_required"]]
)
def test_when_the_case_names_no_agent_then_a_deny_must_hold_on_every_column(
    tmp_path, expect
) -> None:
    ws = make_modules_workspace(tmp_path, SPLIT)
    deny = {"role": "support", **REFUND, "expect": expect}
    [check] = decision_checks(role_wide_columns(ws), [deny])
    assert not check.passed
    assert "(column *)" in check.detail  # "*" allows it


def test_when_the_case_names_no_agent_then_a_superset_must_hold_on_every_column(
    tmp_path,
) -> None:
    # On "*" support may refund like billing; shop-bot's own cell narrows it.
    roles = (
        '  support:\n    "*": [read_only, payments]\n    shop-bot: [read_only]\n'
        "  billing: [read_only, payments]\n"
    )
    ws = make_modules_workspace(tmp_path, roles)
    columns = role_wide_columns(ws)
    superset = {"narrower": "billing", "wider": "support", "probes": [REFUND]}
    [check] = superset_checks(columns, [superset])
    assert check.detail == "refund_order: billing=allow, support=deny (column shop-bot)"


def test_when_an_agents_column_is_invalid_then_score_fails_valid_and_every_dry_run(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path, OPS_COLUMN_PERMISSIVE_DEFAULT)
    case = {"expect": {"decisions": [{"role": "billing", **VIEW, "expect": "allow"}]}}
    checks = by_name(score(case, ws, snapshot(ws), ""))
    assert checks["valid"].detail.startswith("column ops-bot: [permissive-default]")
    # The invalid column's dry-runs can't run, so none passes by being skipped.
    decision = next(c for n, c in checks.items() if n.startswith("decision"))
    assert (decision.passed, decision.detail) == (False, "policy invalid")


def test_when_the_case_names_no_agent_then_name_checks_read_every_agents_attributes_on_every_column(
    tmp_path,
) -> None:
    # ops-bot's own column reads its own attribute (`region`): fine for a
    # project-wide edit; an attribute no agent sends fails on that column too.
    roles = '  billing:\n    "*": [read_only]\n    ops-bot: [ops]\n'
    ws = make_modules_workspace(tmp_path, roles)
    ops = ws / "policies" / "capabilities" / "ops.yaml"
    ops.write_text(
        "tools:\n"
        "  wire_transfer: { mode: allow, constraints: ['ctx.region == \"eu\"'] }\n"
    )
    before = snapshot(ws)
    assert all(c.passed for c in score({}, ws, before, "") if c.name in NAME_CHECKS)
    ops.write_text(ops.read_text().replace("ctx.region", "ctx.regoin"))
    checks = by_name(score({}, ws, before, ""))
    assert checks[NAME_CHECKS[1]].detail == (
        "[unknown-attribute] wire_transfer: ctx.regoin: no audit.json row sends it"
    )


def test_when_a_case_with_no_agent_is_on_a_policy_file_then_both_name_checks_fail(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path)
    policy = valid_policy(ws)
    files = snapshot(ws)
    checks = name_checks(policy, ws, files, files)
    assert [(c.passed, c.detail) for c in checks] == [
        (False, "a case without an agent needs a module tree")
    ] * 2


def test_when_the_case_names_no_agent_then_an_approval_must_hold_on_every_column(
    tmp_path,
) -> None:
    # shop-bot needs approval to refund; any other agent may refund freely.
    roles = '  support:\n    "*": [read_only, payments]\n    shop-bot: [read_only, approvals]\n'
    ws = make_modules_workspace(tmp_path, roles)
    (ws / "policies" / "capabilities" / "approvals.yaml").write_text(
        "tools:\n  refund_order: { mode: approval_required }\n"
    )
    approval = {"role": "support", **REFUND, "expect": "approval_required"}
    [check] = decision_checks(role_wide_columns(ws), [approval])
    assert not check.passed
    assert check.detail.startswith("expected approval_required, got allow")


def test_when_the_case_names_no_agent_then_score_dry_runs_every_column(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path, SPLIT)
    case = {"expect": {"decisions": [{"role": "support", **REFUND, "expect": "deny"}]}}
    checks = by_name(score(case, ws, snapshot(ws), ""))
    decision = next(c for n, c in checks.items() if n.startswith("decision"))
    assert not decision.passed
    assert decision.detail.endswith("(column *)")


def test_when_the_case_names_no_agent_and_no_agent_has_a_manifest_then_both_name_checks_fail(
    tmp_path,
) -> None:
    # The drift lints would check nothing, so an invented tool would pass.
    ws = make_modules_workspace(tmp_path)
    views = json.loads((ws / "agents.json").read_text())
    (ws / "agents.json").write_text(
        json.dumps([{**v, "manifest": None} for v in views])
    )
    files = snapshot(ws)
    checks = name_checks(valid_policy(ws, None, True), ws, files, files)
    assert [(c.passed, c.detail) for c in checks] == [
        (False, "no agent in agents.json has a manifest")
    ] * 2
