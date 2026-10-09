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
    REGISTERED,
    ROLES,
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
# that can make the call, anything else for every agent that might


def _cells(*rows: str) -> str:
    return "".join(f"  {row}\n" for row in rows)


# Refunds on "*" but not in shop-bot's own cell, which replaces "*" for it.
SPLIT = _cells("support:", '  "*": [read_only, payments]', "  shop-bot: [read_only]")
APPROVALS = "tools:\n  refund_order: { mode: approval_required }\n"
PDF = skill_key("resource", "pdf")


def _pdf(mode: str) -> str:
    return f"skills:\n  pdf: {{ mode: {mode}, via: [resource] }}\n"


def _reach(target: str, mode: str, via: str) -> str:
    return f"agents:\n  {target}: {{ mode: {mode}, via: [{via}] }}\n"


def _set(**fields):
    return lambda manifest: manifest.update(fields)


def _call(tool: str, expect, role: str = "support") -> dict:
    args = REFUND["args"] if tool == "refund_order" else {}
    return {"role": role, "tool": tool, "args": args, "expect": expect}


NO_CALLER = "no agent in agents.json can call {}"
OWN_REFUNDS = _cells(
    "support:", '  "*": [read_only]', "  shop-bot: [read_only, payments]"
)
STAR_REFUNDS = _cells(
    "support:", '  "*": [read_only, payments]', "  ops-bot: [read_only]"
)
REACH_OPS = _cells("support:", '  "*": [read_only, reach_ops]')


def _row(roles, call, passed, shown=(), hidden=(), caps=None, edits=None):
    """One role-wide decision: `shown` must be in its detail, `hidden` not."""
    return roles, caps or {}, edits or {}, call, passed, shown, hidden


def _edge(via: str):
    return {AGENT: _set(subagents=[{"name": "ops-bot", "via": via}])}


SECOND_OWNER = {"ops-bot": lambda m: m["tools"].append(manifest_tool("refund_order"))}
PDF_SPLIT = _cells(
    "support:", '  "*": [read_only, pdf]', "  shop-bot: [read_only, pdf_approval]"
)
REACH_SPLIT = _cells(
    "support:", '  "*": [read_only, reach_ops]', "  shop-bot: [read_only, reach_draft]"
)
REACH_BOTH = {
    "reach_ops": _reach("ops-bot", "allow", "handoff"),
    "reach_draft": _reach("draft-bot", "allow", "handoff"),
}
EGRESS_STAR = _cells(
    "support:",
    '  "*": [read_only, egress]',
    "  shop-bot: [read_only]",
    "  ops-bot: [read_only]",
    "  draft-bot: [read_only]",
)
EGRESS = {"egress": "tools:\n  net.http_request: { mode: allow }\n"}
ALLOW, APPROVAL = "allow", "approval_required"
REFUND_CALL = _call("refund_order", ALLOW)
STAR_DRAFT = ["(agent *)", "(agent draft-bot)"]


def _approvals(*cells: str) -> str:
    return _cells("support:", *cells)


ROLE_WIDE_DECISIONS = {
    # An allow needs one agent that can make the call; "*" alone never counts.
    "allow on own cell": _row(OWN_REFUNDS, REFUND_CALL, True),
    "allow via star": _row(STAR_REFUNDS, REFUND_CALL, True),
    "allow or approval on own cell": _row(
        OWN_REFUNDS, _call("refund_order", [ALLOW, APPROVAL]), True
    ),
    "allow or approval via star": _row(
        STAR_REFUNDS, _call("refund_order", [ALLOW, APPROVAL]), True
    ),
    "allow only on star": _row(
        SPLIT,
        REFUND_CALL,
        False,
        ["(agent shop-bot)"],
        ["(agent *)", "(agent ops-bot)"],
    ),
    "allow or approval only on star": _row(
        SPLIT, _call("refund_order", [ALLOW, APPROVAL]), False, ["(agent shop-bot)"]
    ),
    "allow by a second owner": _row(SPLIT, REFUND_CALL, True, edits=SECOND_OWNER),
    "skill allow only on star": _row(
        PDF_SPLIT,
        _call(PDF, ALLOW),
        False,
        ["(agent shop-bot)"],
        caps={"pdf": _pdf(ALLOW), "pdf_approval": _pdf(APPROVAL)},
    ),
    "handoff allow only on star": _row(
        REACH_SPLIT,
        _call("agent.handoff:ops-bot", ALLOW),
        False,
        ["(agent shop-bot)"],
        caps=REACH_BOTH,
        edits=_edge("handoff"),
    ),
    "handoff on a tool edge": _row(
        REACH_OPS,
        _call("agent.handoff:ops-bot", ALLOW),
        False,
        [NO_CALLER.format("agent.handoff:ops-bot")],
        caps={"reach_ops": _reach("ops-bot", ALLOW, "handoff")},
        edits=_edge("tool"),
    ),
    # LangGraph hides agent-as-tool edges and gates them; Pydantic AI never gates.
    **{
        f"hidden tool edge, {fw}": _row(
            REACH_OPS,
            _call("agent.tool:ops-bot", ALLOW),
            ok,
            caps={"reach_ops": _reach("ops-bot", ALLOW, "tool")},
            edits={AGENT: _set(framework=fw), "ops-bot": _set(framework=fw)},
        )
        for fw, ok in [("langchain", True), ("openai", False), ("pydantic-ai", False)]
    },
    "hidden edge to no registered agent": _row(
        ROLES,
        _call("agent.tool:opsbot", "deny", "billing"),
        False,
        [NO_CALLER.format("agent.tool:opsbot")],
    ),
    "hidden edge only to itself": _row(
        ROLES,
        _call("agent.tool:ops-bot", "deny", "billing"),
        False,
        [NO_CALLER.format("agent.tool:ops-bot")],
        edits={AGENT: _set(framework="openai")},
    ),
    **{
        f"no agent has the tool, {e}": _row(
            ROLES,
            _call("refund_orders", e, "billing"),
            False,
            [NO_CALLER.format("refund_orders")],
        )
        for e in [ALLOW, APPROVAL, ("deny", APPROVAL)]
    },
    "admission is every agent's": _row(
        _approvals('  "*": [read_only, admit]', "  shop-bot: [read_only]"),
        _call("agent.run", ALLOW),
        True,
        caps={"admit": "admission: { mode: allow }\n"},
    ),
    "egress allowed on star only": _row(
        EGRESS_STAR,
        _call("net.http_request", ALLOW),
        False,
        ["(agent shop-bot)"],
        ["(agent *)"],
        caps=EGRESS,
    ),
    # Anything else holds for every agent that might make the call: one that
    # can, one with no manifest, and "*" (an agent not registered yet).
    **{
        f"deny, {e}": _row(
            SPLIT,
            _call("refund_order", e),
            False,
            STAR_DRAFT,
            ["(agent ops-bot)", "(agent shop-bot)"],
        )
        for e in ["deny", ("deny", APPROVAL)]
    },
    "deny with an own cell and no manifest": _row(
        _approvals('  "*": [read_only]', "  draft-bot: [read_only, payments]"),
        _call("refund_order", "deny"),
        False,
        ["(agent draft-bot)"],
    ),
    "approval allowed elsewhere": _row(
        _approvals(
            '  "*": [read_only, payments]', "  shop-bot: [read_only, approvals]"
        ),
        _call("refund_order", APPROVAL),
        False,
        ["expected approval_required, got allow", "(agent *)"],
        ["(agent shop-bot)"],
        caps={"approvals": APPROVALS},
    ),
    "approval denied by star": _row(
        _approvals('  "*": [read_only]', "  shop-bot: [read_only, approvals]"),
        _call("refund_order", APPROVAL),
        False,
        ["(agent *)"],
        caps={"approvals": APPROVALS},
    ),
    "approval denied by an agent with the tool": _row(
        _approvals('  "*": [read_only, approvals]', "  shop-bot: [read_only]"),
        _call("refund_order", APPROVAL),
        False,
        ["expected approval_required, got deny", "(agent shop-bot)"],
        caps={"approvals": APPROVALS},
    ),
    "approval denied by an agent without the tool": _row(
        _approvals('  "*": [read_only, approvals]', "  ops-bot: [read_only]"),
        _call("refund_order", APPROVAL),
        True,
        caps={"approvals": APPROVALS},
    ),
    "skill approval undeclared without the skill": _row(
        _approvals('  "*": [read_only, pdf]', "  ops-bot: [read_only]"),
        _call(PDF, APPROVAL),
        True,
        caps={"pdf": _pdf(APPROVAL)},
    ),
    "skill approval undeclared on star": _row(
        _approvals('  "*": [read_only]', "  shop-bot: [read_only, pdf]"),
        _call(PDF, APPROVAL),
        False,
        ["can't dry-run", "(agent *)"],
        caps={"pdf": _pdf(APPROVAL)},
    ),
    **{
        f"{via} approval undeclared without the edge": _row(
            _approvals('  "*": [read_only, reach_ops]', "  ops-bot: [read_only]"),
            _call(f"agent.{via}:ops-bot", APPROVAL),
            True,
            caps={"reach_ops": _reach("ops-bot", APPROVAL, via)},
            edits=_edge(via),
        )
        for via in ["handoff", "tool"]
    },
}


def _role_wide(tmp_path, roles: str, capabilities: dict, edits: dict) -> dict:
    ws = make_modules_workspace(tmp_path, roles)
    for name, body in capabilities.items():
        write_capability(ws, name, body)
    for agent, edit in edits.items():
        edit_manifest(ws, agent, edit)
    return role_wide_policies(ws)


@pytest.mark.parametrize(
    ("roles", "capabilities", "edits", "decision", "passed", "shown", "hidden"),
    list(ROLE_WIDE_DECISIONS.values()),
    ids=list(ROLE_WIDE_DECISIONS),
)
def test_when_the_case_names_no_agent_then_decisions_hold_per_agent(
    tmp_path, roles, capabilities, edits, decision, passed, shown, hidden
) -> None:
    if isinstance(decision["expect"], tuple):
        decision = {**decision, "expect": list(decision["expect"])}
    policies = _role_wide(tmp_path, roles, capabilities, edits)
    [check] = decision_checks(policies, [decision], REGISTERED)
    assert check.passed is passed, check.detail
    assert (check.detail == "") if passed else check.detail
    assert all(s in check.detail for s in shown)
    assert not any(s in check.detail for s in hidden)


BILLING = "  billing: [read_only, payments]\n"
REFUND_SUPERSET = {"narrower": "billing", "wider": "support", "probes": [REFUND]}


@pytest.mark.parametrize(
    ("roles", "superset", "detail"),
    [
        pytest.param(
            SPLIT + BILLING,
            REFUND_SUPERSET,
            "refund_order: billing=allow, support=deny (agent shop-bot)",
            id="own cell narrows",
        ),
        pytest.param(
            STAR_REFUNDS + BILLING,
            REFUND_SUPERSET,
            "",
            id="agent without the tool skipped",
        ),
        pytest.param(
            OWN_REFUNDS + BILLING,
            REFUND_SUPERSET,
            "refund_order: billing=allow, support=deny (agent *); "
            "refund_order: billing=allow, support=deny (agent draft-bot)",
            id="star narrows",
        ),
        pytest.param(
            _cells(
                "support:", '  "*": [read_only, payments]', "  draft-bot: [read_only]"
            )
            + BILLING,
            REFUND_SUPERSET,
            "refund_order: billing=allow, support=deny (agent draft-bot)",
            id="no manifest narrows",
        ),
        pytest.param(
            ROLES,
            {
                "narrower": "default",
                "wider": "billing",
                "probes": [{"tool": "refund_orders", "args": {}}],
            },
            NO_CALLER.format("refund_orders"),
            id="no agent can make the probe",
        ),
    ],
)
def test_when_the_case_names_no_agent_then_supersets_hold_per_agent(
    tmp_path, roles, superset, detail
) -> None:
    [check] = superset_checks(
        _role_wide(tmp_path, roles, {}, {}), [superset], REGISTERED
    )
    assert (check.passed, check.detail) == (not detail, detail)


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
