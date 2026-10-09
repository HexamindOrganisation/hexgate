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
from hexgate.security.models import skill_key
from tests.evals.helpers import (
    AGENT,
    OPS_COLUMN_PERMISSIVE_DEFAULT,
    PERMISSIVE_DEFAULT,
    POLICY,
    by_name,
    edit_manifest,
    install_skill,
    make_modules_workspace,
    make_workspace,
    manifest_tool,
    role_wide_policies,
    valid_policy,
    write_capability,
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


# A role or project-wide case (no agent) on a module tree: an allow for one agent
# that has the tool, anything else for every agent


# Refunds on "*" but not in shop-bot's own cell, which replaces "*" for it.
SPLIT = '  support:\n    "*": [read_only, payments]\n    shop-bot: [read_only]\n'


@pytest.mark.parametrize("expect", ["allow", ["allow", "approval_required"]])
@pytest.mark.parametrize(
    ("roles", "passed"),
    [
        # shop-bot, the only agent with refund_order, refunds on its own cell.
        (
            '  support:\n    "*": [read_only]\n    shop-bot: [read_only, payments]\n',
            True,
        ),
        # With no cell of its own, shop-bot runs on "*", which refunds.
        (
            '  support:\n    "*": [read_only, payments]\n    ops-bot: [read_only]\n',
            True,
        ),
        # Only "*" refunds, and shop-bot's own cell replaces it: no agent can.
        (SPLIT, False),
    ],
)
def test_when_the_case_names_no_agent_then_an_allow_needs_one_agent_that_has_the_tool(
    tmp_path, roles, passed, expect
) -> None:
    ws = make_modules_workspace(tmp_path, roles)
    allow = {"role": "support", **REFUND, "expect": expect}
    [check] = decision_checks(role_wide_policies(ws), [allow], role_wide=True)
    assert check.passed is passed
    if passed:
        assert check.detail == ""
    else:  # ops-bot and "*" refund, but neither has the tool
        assert check.detail.endswith("(agent shop-bot)")
        assert "(agent *)" not in check.detail and "(agent ops-bot)" not in check.detail


def test_when_two_agents_have_the_tool_then_an_allow_needs_only_one(tmp_path) -> None:
    # ops-bot gets refund_order too and runs on "*", which refunds.
    ws = make_modules_workspace(tmp_path, SPLIT)
    edit_manifest(
        ws, "ops-bot", lambda m: m["tools"].append(manifest_tool("refund_order"))
    )
    allow = {"role": "support", **REFUND, "expect": "allow"}
    [check] = decision_checks(role_wide_policies(ws), [allow], role_wide=True)
    assert (check.passed, check.detail) == (True, "")


def test_when_the_case_names_no_agent_then_a_skill_allow_needs_an_agent_with_the_skill(
    tmp_path,
) -> None:
    # Only shop-bot lists the pdf skill, and its own cell only grants it with
    # approval: "*" allowing it gives no agent that has it a free read.
    roles = '  support:\n    "*": [read_only, pdf]\n    shop-bot: [read_only, pdf_approval]\n'
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(ws, "pdf", "skills:\n  pdf: { mode: allow, via: [resource] }\n")
    write_capability(
        ws,
        "pdf_approval",
        "skills:\n  pdf: { mode: approval_required, via: [resource] }\n",
    )
    read = {"role": "support", "tool": skill_key("resource", "pdf"), "expect": "allow"}
    [check] = decision_checks(role_wide_policies(ws), [read], role_wide=True)
    assert not check.passed
    assert check.detail.endswith("(agent shop-bot)")


@pytest.mark.parametrize(
    ("via", "detail"),
    [
        # Only shop-bot hands off to ops-bot, and its own cell reaches only
        # draft-bot: "*" reaching ops-bot lets no agent with the edge use it.
        ("handoff", "(agent shop-bot)"),
        # shop-bot's edge is agent-as-tool; ops-bot records none: no agent can.
        ("tool", "no agent in agents.json can call agent.handoff:ops-bot"),
    ],
)
def test_when_the_case_names_no_agent_then_a_reach_allow_needs_an_agent_with_that_sub_agent(
    tmp_path, via, detail
) -> None:
    roles = (
        '  support:\n    "*": [read_only, reach_ops]\n'
        "    shop-bot: [read_only, reach_draft]\n"
    )
    ws = make_modules_workspace(tmp_path, roles)
    for name, target in [("reach_ops", "ops-bot"), ("reach_draft", "draft-bot")]:
        write_capability(
            ws, name, f"agents:\n  {target}: {{ mode: allow, via: [handoff] }}\n"
        )
    edge = {"name": "ops-bot", "via": via}
    edit_manifest(ws, AGENT, lambda m: m.update(subagents=[edge]))
    handoff = {"role": "support", "tool": "agent.handoff:ops-bot", "expect": "allow"}
    [check] = decision_checks(role_wide_policies(ws), [handoff], role_wide=True)
    assert not check.passed
    assert check.detail.endswith(detail)


@pytest.mark.parametrize(
    ("framework", "passed"),
    [("langchain", True), ("openai", False), ("pydantic-ai", False)],
)
def test_when_a_framework_hides_agent_as_tool_edges_then_its_agent_may_call(
    tmp_path, framework, passed
) -> None:
    # No agent records an edge to ops-bot, but a LangGraph agent's agent-as-tool
    # edges hide in a tool closure: shop-bot, on "*", may still reach it. A
    # Pydantic AI agent hides them too, but never gates them.
    ws = make_modules_workspace(
        tmp_path, '  support:\n    "*": [read_only, reach_ops]\n'
    )
    write_capability(
        ws, "reach_ops", "agents:\n  ops-bot: { mode: allow, via: [tool] }\n"
    )
    for agent in (AGENT, "ops-bot"):
        edit_manifest(ws, agent, lambda m: m.update(framework=framework))
    call = {"role": "support", "tool": "agent.tool:ops-bot", "expect": "allow"}
    [check] = decision_checks(role_wide_policies(ws), [call], role_wide=True)
    assert check.passed is passed


def test_when_no_agent_has_the_tool_then_a_role_wide_allow_fails(tmp_path) -> None:
    # A tool in no manifest (here, one a case invents): no agent can call it,
    # even where "*" or an agent's cell allows it.
    ws = make_modules_workspace(tmp_path, '  support:\n    "*": [read_only, ghost]\n')
    write_capability(ws, "ghost", "tools:\n  ghost_tool: { mode: allow }\n")
    call = {"role": "support", "tool": "ghost_tool", "expect": "allow"}
    [check] = decision_checks(role_wide_policies(ws), [call], role_wide=True)
    assert (check.passed, check.detail) == (
        False,
        "no agent in agents.json can call ghost_tool",
    )


@pytest.mark.parametrize(
    "expect", ["allow", "approval_required", ["deny", "approval_required"]]
)
def test_when_no_agent_can_call_the_key_then_any_role_wide_decision_fails(
    tmp_path, expect
) -> None:
    # A typo'd tool no manifest lists: every agent denies it, but that says
    # nothing about the call the case meant.
    ws = make_modules_workspace(tmp_path)
    call = {"role": "billing", "tool": "refund_orders", "expect": expect}
    [check] = decision_checks(role_wide_policies(ws), [call], role_wide=True)
    assert (check.passed, check.detail) == (
        False,
        "no agent in agents.json can call refund_orders",
    )


def test_when_the_case_names_no_agent_then_admission_is_every_agents(tmp_path) -> None:
    # agent.run is in no manifest, but every registered agent is admitted.
    roles = '  support:\n    "*": [read_only, admit]\n    shop-bot: [read_only]\n'
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(ws, "admit", "admission: { mode: allow }\n")
    call = {"role": "support", "tool": "agent.run", "expect": "allow"}
    [check] = decision_checks(role_wide_policies(ws), [call], role_wide=True)
    assert (check.passed, check.detail) == (True, "")


def test_when_an_agent_cant_make_a_probe_then_a_role_wide_superset_skips_it(
    tmp_path,
) -> None:
    # ops-bot's own support cell lacks refunds, but ops-bot has no refund_order.
    roles = (
        '  support:\n    "*": [read_only, payments]\n    ops-bot: [read_only]\n'
        "  billing: [read_only, payments]\n"
    )
    ws = make_modules_workspace(tmp_path, roles)
    superset = {"narrower": "billing", "wider": "support", "probes": [REFUND]}
    [check] = superset_checks(role_wide_policies(ws), [superset], role_wide=True)
    assert (check.passed, check.detail) == (True, "")


def test_when_no_agent_can_make_a_probe_then_a_role_wide_superset_fails(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path)
    probe = {"tool": "refund_orders", "args": {}}
    superset = {"narrower": "default", "wider": "billing", "probes": [probe]}
    [check] = superset_checks(role_wide_policies(ws), [superset], role_wide=True)
    assert (check.passed, check.detail) == (
        False,
        "no agent in agents.json can call refund_orders",
    )


def test_when_star_narrows_the_wider_role_then_a_role_wide_superset_fails(
    tmp_path,
) -> None:
    # "*" stands for agents not registered yet: support there can't refund
    # while billing can, though shop-bot's own cell keeps the superset.
    roles = (
        '  support:\n    "*": [read_only]\n    shop-bot: [read_only, payments]\n'
        "  billing: [read_only, payments]\n"
    )
    ws = make_modules_workspace(tmp_path, roles)
    superset = {"narrower": "billing", "wider": "support", "probes": [REFUND]}
    [check] = superset_checks(role_wide_policies(ws), [superset], role_wide=True)
    assert not check.passed
    assert "(agent *)" in check.detail


def test_when_a_hidden_reach_targets_no_registered_agent_then_no_agent_can_call(
    tmp_path,
) -> None:
    # A LangGraph agent may hide agent-as-tool edges, but only to an agent in
    # agents.json, so a typo'd target still names a call no agent makes.
    ws = make_modules_workspace(tmp_path)
    call = {"role": "billing", "tool": "agent.tool:opsbot", "expect": "deny"}
    [check] = decision_checks(role_wide_policies(ws), [call], role_wide=True)
    assert (check.passed, check.detail) == (
        False,
        "no agent in agents.json can call agent.tool:opsbot",
    )


def test_when_only_the_target_hides_its_edges_then_no_agent_can_call(tmp_path) -> None:
    # Only ops-bot is LangGraph, and an agent doesn't reach itself as a tool.
    ws = make_modules_workspace(tmp_path)
    edit_manifest(ws, AGENT, lambda m: m.update(framework="openai"))
    call = {"role": "billing", "tool": "agent.tool:ops-bot", "expect": "deny"}
    [check] = decision_checks(role_wide_policies(ws), [call], role_wide=True)
    assert (check.passed, check.detail) == (
        False,
        "no agent in agents.json can call agent.tool:ops-bot",
    )


def test_when_an_agent_without_the_skill_never_declares_it_then_an_approval_holds(
    tmp_path,
) -> None:
    # ops-bot has no pdf skill and keeps its own cell, which declares no skills:
    # it never reads pdf, so its policy needn't govern it.
    roles = '  support:\n    "*": [read_only, pdf]\n    ops-bot: [read_only]\n'
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(
        ws, "pdf", "skills:\n  pdf: { mode: approval_required, via: [resource] }\n"
    )
    read = {
        "role": "support",
        "tool": skill_key("resource", "pdf"),
        "expect": "approval_required",
    }
    [check] = decision_checks(role_wide_policies(ws), [read], role_wide=True)
    assert (check.passed, check.detail) == (True, "")


@pytest.mark.parametrize("via", ["handoff", "tool"])
def test_when_an_agent_without_the_edge_never_declares_reach_then_an_approval_holds(
    tmp_path, via
) -> None:
    # Only shop-bot (on "*") reaches ops-bot; ops-bot keeps its own cell with no
    # `agents:` rule, but it never makes the call, so it isn't judged.
    roles = '  support:\n    "*": [read_only, reach_ops]\n    ops-bot: [read_only]\n'
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(
        ws,
        "reach_ops",
        f"agents:\n  ops-bot: {{ mode: approval_required, via: [{via}] }}\n",
    )
    edit_manifest(
        ws, AGENT, lambda m: m.update(subagents=[{"name": "ops-bot", "via": via}])
    )
    call = {
        "role": "support",
        "tool": f"agent.{via}:ops-bot",
        "expect": "approval_required",
    }
    [check] = decision_checks(role_wide_policies(ws), [call], role_wide=True)
    assert (check.passed, check.detail) == (True, "")


# draft-bot is registered with no manifest, and its own cell grants refunds.
DRAFT_REFUNDS = (
    '  support:\n    "*": [read_only]\n    draft-bot: [read_only, payments]\n'
)


def test_when_an_agent_with_no_manifest_allows_it_then_a_role_wide_deny_fails(
    tmp_path,
) -> None:
    # No manifest says draft-bot can't refund, so its cell must deny too.
    ws = make_modules_workspace(tmp_path, DRAFT_REFUNDS)
    deny = {"role": "support", **REFUND, "expect": "deny"}
    [check] = decision_checks(role_wide_policies(ws), [deny], role_wide=True)
    assert not check.passed
    assert check.detail.endswith("(agent draft-bot)")


def test_when_an_agent_with_no_manifest_narrows_a_role_then_a_role_wide_superset_fails(
    tmp_path,
) -> None:
    ws = make_modules_workspace(
        tmp_path,
        '  support:\n    "*": [read_only, payments]\n    draft-bot: [read_only]\n'
        "  billing: [read_only, payments]\n",
    )
    superset = {"narrower": "billing", "wider": "support", "probes": [REFUND]}
    [check] = superset_checks(role_wide_policies(ws), [superset], role_wide=True)
    assert check.detail == "refund_order: billing=allow, support=deny (agent draft-bot)"


def test_when_star_never_declares_the_skill_then_an_approval_fails(tmp_path) -> None:
    # "*" declares no skills, so an agent not registered yet would read pdf
    # ungated: that isn't "needs approval".
    roles = '  support:\n    "*": [read_only]\n    shop-bot: [read_only, pdf]\n'
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(
        ws, "pdf", "skills:\n  pdf: { mode: approval_required, via: [resource] }\n"
    )
    read = {
        "role": "support",
        "tool": skill_key("resource", "pdf"),
        "expect": "approval_required",
    }
    [check] = decision_checks(role_wide_policies(ws), [read], role_wide=True)
    assert not check.passed
    assert check.detail.startswith("can't dry-run") and "(agent *)" in check.detail


def test_when_star_denies_what_needs_approval_then_the_approval_fails(tmp_path) -> None:
    # shop-bot's own cell needs approval, but "*" denies: an agent registered
    # later with refund_order would be denied, not asked for approval.
    roles = '  support:\n    "*": [read_only]\n    shop-bot: [read_only, approvals]\n'
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(
        ws, "approvals", "tools:\n  refund_order: { mode: approval_required }\n"
    )
    approval = {"role": "support", **REFUND, "expect": "approval_required"}
    [check] = decision_checks(role_wide_policies(ws), [approval], role_wide=True)
    assert not check.passed
    assert "(agent *)" in check.detail


def test_when_a_case_with_no_agent_is_on_a_policy_file_then_its_one_policy_is_dry_run(
    tmp_path,
) -> None:
    # Not role-wide: a policy file is one agent's, so no per-agent tags.
    ws = make_workspace(tmp_path)
    case = {"expect": {"decisions": [{"role": "billing", **REFUND, "expect": "deny"}]}}
    decision = next(
        c for c in score(case, ws, snapshot(ws), "") if c.name.startswith("decision")
    )
    assert not decision.passed
    assert decision.detail.startswith("expected deny, got allow")
    assert "(agent" not in decision.detail


def test_when_an_agent_with_the_tool_denies_it_then_an_approval_fails(tmp_path) -> None:
    # shop-bot can refund, so its own cell denying it doesn't meet "needs approval".
    roles = '  support:\n    "*": [read_only, approvals]\n    shop-bot: [read_only]\n'
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(
        ws, "approvals", "tools:\n  refund_order: { mode: approval_required }\n"
    )
    approval = {"role": "support", **REFUND, "expect": "approval_required"}
    [check] = decision_checks(role_wide_policies(ws), [approval], role_wide=True)
    assert not check.passed
    assert check.detail.startswith("expected approval_required, got deny")
    assert check.detail.endswith("(agent shop-bot)")


def test_when_an_agent_without_the_tool_has_its_own_cell_then_it_isnt_judged(
    tmp_path,
) -> None:
    # Support needs approval to refund: "*" grants it with approval, and ops-bot,
    # which has no refund_order, keeps its own cell, which denies it.
    roles = '  support:\n    "*": [read_only, approvals]\n    ops-bot: [read_only]\n'
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(
        ws, "approvals", "tools:\n  refund_order: { mode: approval_required }\n"
    )
    approval = {"role": "support", **REFUND, "expect": "approval_required"}
    [check] = decision_checks(role_wide_policies(ws), [approval], role_wide=True)
    assert (check.passed, check.detail) == (True, "")


def test_when_no_agent_lists_the_key_then_an_allow_on_star_alone_fails(
    tmp_path,
) -> None:
    # Egress is no manifest's: any registered agent counts, but "*" stands for
    # an agent not registered yet, and every registered one has its own cell.
    roles = (
        '  support:\n    "*": [read_only, egress]\n    shop-bot: [read_only]\n'
        "    ops-bot: [read_only]\n    draft-bot: [read_only]\n"
    )
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(ws, "egress", "tools:\n  net.http_request: { mode: allow }\n")
    call = {"role": "support", "tool": "net.http_request", "expect": "allow"}
    [check] = decision_checks(role_wide_policies(ws), [call], role_wide=True)
    assert not check.passed
    assert "(agent *)" not in check.detail and "(agent shop-bot)" in check.detail


@pytest.mark.parametrize("expect", ["deny", ["deny", "approval_required"]])
def test_when_the_case_names_no_agent_then_a_deny_must_hold_for_every_agent(
    tmp_path, expect
) -> None:
    ws = make_modules_workspace(tmp_path, SPLIT)
    deny = {"role": "support", **REFUND, "expect": expect}
    [check] = decision_checks(role_wide_policies(ws), [deny], role_wide=True)
    assert not check.passed
    # "*" refunds, and an agent registered later could have the tool; ops-bot,
    # also on "*", has none, so it isn't judged.
    # draft-bot, on "*" with no manifest, may refund too; ops-bot can't.
    assert "(agent *)" in check.detail and "(agent draft-bot)" in check.detail
    assert "(agent ops-bot)" not in check.detail


def test_when_the_case_names_no_agent_then_an_approval_must_hold_for_every_agent(
    tmp_path,
) -> None:
    # shop-bot needs approval to refund; any other agent may refund freely.
    roles = '  support:\n    "*": [read_only, payments]\n    shop-bot: [read_only, approvals]\n'
    ws = make_modules_workspace(tmp_path, roles)
    write_capability(
        ws, "approvals", "tools:\n  refund_order: { mode: approval_required }\n"
    )
    approval = {"role": "support", **REFUND, "expect": "approval_required"}
    [check] = decision_checks(role_wide_policies(ws), [approval], role_wide=True)
    assert not check.passed
    assert check.detail.startswith("expected approval_required, got allow")
    assert "(agent *)" in check.detail and "(agent shop-bot)" not in check.detail


def test_when_the_case_names_no_agent_then_a_superset_must_hold_for_every_agent(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path, SPLIT + "  billing: [read_only, payments]\n")
    superset = {"narrower": "billing", "wider": "support", "probes": [REFUND]}
    [check] = superset_checks(role_wide_policies(ws), [superset], role_wide=True)
    assert check.detail == "refund_order: billing=allow, support=deny (agent shop-bot)"


def test_when_an_agents_policy_is_invalid_then_score_fails_valid_and_every_dry_run(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path, OPS_COLUMN_PERMISSIVE_DEFAULT)
    case = {"expect": {"decisions": [{"role": "billing", **VIEW, "expect": "allow"}]}}
    checks = by_name(score(case, ws, snapshot(ws), ""))
    assert checks["valid"].detail.startswith("agent ops-bot: [permissive-default]")
    # The invalid agent's dry-runs can't run, so none passes by being skipped.
    decision = next(c for n, c in checks.items() if n.startswith("decision"))
    assert (decision.passed, decision.detail) == (False, "policy invalid")


def test_when_the_case_names_no_agent_then_name_checks_read_every_agents_attributes(
    tmp_path,
) -> None:
    # ops-bot's own cell reads its own attribute (`region`): fine for a
    # project-wide edit; an attribute no agent sends fails there too.
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


@pytest.mark.parametrize(
    ("expect", "missed", "spared"),
    [
        ("allow", ["shop-bot"], ["*", "ops-bot", "draft-bot"]),  # shop-bot only
        ("deny", ["*", "draft-bot"], ["shop-bot", "ops-bot"]),  # who might call
    ],
)
def test_when_the_case_names_no_agent_then_score_dry_runs_per_agent(
    tmp_path, expect, missed, spared
) -> None:
    ws = make_modules_workspace(tmp_path, SPLIT)
    case = {"expect": {"decisions": [{"role": "support", **REFUND, "expect": expect}]}}
    checks = by_name(score(case, ws, snapshot(ws), ""))
    decision = next(c for n, c in checks.items() if n.startswith("decision"))
    assert not decision.passed
    assert all(f"(agent {a})" in decision.detail for a in missed)
    assert not any(f"(agent {a})" in decision.detail for a in spared)


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
