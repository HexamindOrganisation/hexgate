"""Invented names (`names.py`), against shop-bot's known names."""

from __future__ import annotations

from dataclasses import replace

import pytest

from evals.policy_writing.names import unknown_keys, unknown_refs
from hexgate.security.policy_set import load_policy_set_from_dict
from tests.evals.helpers import AGENT, KNOWN, make_modules_workspace, valid_policy

ALLOW = {"mode": "allow"}


def loaded(doc: dict):
    return load_policy_set_from_dict({"version": 1, **doc})


def on(tool: str, *constraints: str) -> dict:
    return {"tools": {tool: {"mode": "allow", "constraints": list(constraints)}}}


# unknown_keys


@pytest.mark.parametrize(
    "tool",
    [
        "wire_transfer",  # only in another agent's manifest
        "net.http_reqest",
        "agent.handof:ops-bot",
        "agent.tools:ops-bot",
        "agent.runs",
    ],
)
def test_when_a_tool_is_not_in_the_manifest_then_unknown_keys_flags_it(tool) -> None:
    doc = {"roles": {"billing": on("refund_order"), "ops": on(tool)}}
    assert unknown_keys(loaded(doc), KNOWN) == [tool]


def test_unknown_keys_happy_path() -> None:
    doc = {
        "tools": {"net.http_request": ALLOW, "net.tcp_connect": ALLOW},
        "admission": ALLOW,
        "agents": {"ops-bot": ALLOW},
    }
    assert unknown_keys(loaded(doc), KNOWN) == []


def test_when_a_skill_or_guard_is_not_in_the_manifest_then_unknown_keys_flags_it() -> (
    None
):
    guards = {"redact_pii": {"enabled": True}, "redact_pi": {"enabled": False}}
    doc = {
        "roles": {
            "billing": {"skills": {"pdf": ALLOW}, "guards": guards},
            "ops": {"skills": {"ledger": ALLOW}, "guards": guards},  # ledger: ops-bot's
        }
    }
    assert unknown_keys(loaded(doc), KNOWN) == ["guard:redact_pi", "skill:ledger"]


def test_when_a_skill_name_is_padded_then_unknown_keys_trims_it() -> None:
    assert unknown_keys(loaded({"skills": {" pdf ": ALLOW}}), KNOWN) == []


def test_when_a_module_tree_lowers_agent_and_skill_keys_then_both_accept_them(
    tmp_path,
) -> None:
    # Resolving a module tree lowers admission, reach and skills into `tools`.
    roles = "  default: [read_only]\n  billing: [read_only, reach]\n"
    ws = make_modules_workspace(tmp_path, roles)
    (ws / "policies" / "capabilities" / "reach.yaml").write_text(
        'admission: { mode: allow, constraints: ["args.agent == \\"shop-bot\\""] }\n'
        "agents:\n  ops-bot: { mode: allow }\n"
        "skills:\n  pdf: { mode: allow, via: [script], constraints:"
        ' ["args.script_args == \\"x\\""] }\n'
    )
    policy_set = valid_policy(ws, AGENT, modules=True).policy_set
    assert {"agent.run", "skill.script:pdf"} <= set(
        policy_set.policy_for("billing").tools
    )
    assert (unknown_keys(policy_set, KNOWN), unknown_refs(policy_set, KNOWN)) == (
        [],
        [],
    )


def test_when_a_module_tree_grants_an_invented_skill_then_unknown_keys_flags_it(
    tmp_path,
) -> None:
    roles = "  default: [read_only]\n  billing: [read_only, sk]\n"
    ws = make_modules_workspace(tmp_path, roles)
    (ws / "policies" / "capabilities" / "sk.yaml").write_text(
        "skills:\n  pdff: { mode: allow, via: [resource] }\n"
    )
    policy_set = valid_policy(ws, AGENT, modules=True).policy_set
    assert unknown_keys(policy_set, KNOWN) == ["skill:pdff"]


# unknown_refs


def test_when_an_argument_is_not_in_the_manifest_then_unknown_refs_flags_it() -> None:
    doc = on("refund_order", 'args.tier != "gold" or args.amount <= 1000')
    assert unknown_refs(loaded(doc), KNOWN) == ["refund_order: args.tier"]


@pytest.mark.parametrize(
    ("attribute", "known"), [("department", True), ("team", False)]
)
def test_when_an_attribute_is_in_no_audit_row_then_unknown_refs_flags_it(
    attribute, known
) -> None:
    refs = unknown_refs(loaded(on("refund_order", f'ctx.{attribute} == "x"')), KNOWN)
    assert refs == ([] if known else [f"refund_order: ctx.{attribute}"])


@pytest.mark.parametrize(
    "constraint",
    [
        'user.department == "finance"',
        "arg.amount <= 5",
        "attrs.vip == true",
        'role.name == "x"',
        'tool.name == "x"',
        "role.amount == 1",  # dotted even when the rest names a real argument
        "tool.amount == 1",
    ],
)
def test_when_a_path_never_matches_then_unknown_refs_flags_it(constraint) -> None:
    [ref] = unknown_refs(loaded(on("refund_order", constraint)), KNOWN)
    assert ref == f"refund_order: {constraint.split()[0]}"


def test_unknown_refs_happy_path() -> None:
    constraint = (
        'run.tool_calls < 20 and role == "default" and tool == "refund_order"'
        " and count(args) > 0"
    )
    assert unknown_refs(loaded(on("refund_order", constraint)), KNOWN) == []


def test_when_a_name_is_inside_a_string_then_unknown_refs_ignores_it() -> None:
    # `ctx.x` inside a string is data, and quantifier bodies are walked.
    constraint = 'args.note == "see ctx.x" and any(args.items, .price < 5)'
    known = replace(KNOWN, tools={"refund_order": {"note", "items"}})
    assert unknown_refs(loaded(on("refund_order", constraint)), known) == []


def test_when_a_skill_constraint_reads_another_levels_args_then_unknown_refs_flags_it() -> (
    None
):
    constraints = [
        'startswith(args.file_path, "scripts/") and args.script_args != ""',
        "args.amount <= 500",  # a tool argument, never on a skill call
        "ctx.invented == 1",
    ]
    skill = {"mode": "allow", "via": ["resource", "script"], "constraints": constraints}
    assert unknown_refs(loaded({"skills": {"pdf": skill}}), KNOWN) == [
        "skill.resource:pdf: args.amount",
        "skill.resource:pdf: args.script_args",  # only a script carries it
        "skill.resource:pdf: ctx.invented",
        "skill.script:pdf: args.amount",
        "skill.script:pdf: ctx.invented",
    ]


@pytest.mark.parametrize(
    "doc",
    [
        {"constraints": ['ctx.departmnt == "x"']},  # a flat file's own fence
        {"constraints": ['ctx.departmnt == "x"'], "roles": {"billing": {}}},
        {"roles": {"billing": {"constraints": ['ctx.departmnt == "x"']}}},
        {"default_policy": {"mode": "allow", "constraints": ['ctx.departmnt == "x"']}},
    ],
)
def test_when_a_file_role_or_default_constraint_has_a_typo_then_unknown_refs_flags_it(
    doc,
) -> None:
    assert unknown_refs(loaded(doc), KNOWN) == ["policy-level: ctx.departmnt"]


def test_when_a_policy_level_constraint_reads_an_invented_arg_then_unknown_refs_flags_it() -> (
    None
):
    doc = {"constraints": ["args.amount <= 1000 and args.amont <= 1000"]}
    assert unknown_refs(loaded(doc), KNOWN) == ["policy-level: args.amont"]


@pytest.mark.parametrize(
    "constraint",
    ['args.skill != "shell"', 'args.host != "evil.com"', 'args.target != "x"'],
)
def test_when_a_policy_level_constraint_reads_synthetic_args_then_unknown_refs_accepts_them(
    constraint,
) -> None:
    doc = {"constraints": [constraint], "skills": {"pdf": ALLOW}}
    assert unknown_refs(loaded(doc), KNOWN) == []


def test_when_a_synthetic_key_reads_another_gates_args_then_unknown_refs_flags_it() -> (
    None
):
    def fenced(constraint: str, **fields) -> dict:
        return {"mode": "allow", "constraints": [constraint], **fields}

    doc = {
        "tools": {"net.http_request": fenced('args.method == "GET"')},
        "admission": fenced('args.target == "billing"'),
        "agents": {
            "billing": fenced('args.target == "billing"', via=["tool"]),
            "ops-bot": fenced("args.amount <= 500", via=["handoff"]),
        },
    }
    assert unknown_refs(loaded(doc), KNOWN) == [
        "agent.handoff:ops-bot: args.amount",
        "agent.run: args.target",
    ]
