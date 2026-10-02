"""Known names and invented ones (`names.py`), plus `name_checks` and drift tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from evals.policy_writing.checks import name_checks, snapshot
from evals.policy_writing.names import (
    AGENT_REACH_ARGS,
    SKILL_ARGS,
    SYNTHETIC_ARGS,
    invented_names,
    load_known_names,
    unknown_refs,
    unknown_tools,
)
from evals.policy_writing.policy import effective_policy
from hexgate.adapters.google.tools import _skill_decision
from hexgate.adapters.langchain.skills import _SkillRead
from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.egress.tcp import TcpEgressProxy
from hexgate.manifest.langchain import SkillLocation
from hexgate.runtime.context import HexgateContext
from hexgate.security import resolve_agent_gate, resolve_reach_gate
from hexgate.security.enforcer import build_enforcer
from hexgate.security.policy_set import load_policy_set_from_dict
from tests.evals.helpers import (
    AGENT,
    AUDIT,
    POLICY,
    add_boundary_tool,
    by_name,
    make_modules_workspace,
    make_workspace,
    manifest_tool,
    write_module,
)

# shop-bot's manifest, as `load_known_names` reads it from the fixture.
TOOLS = {"view_orders": {"customer_id"}, "refund_order": {"order_id", "amount"}}


def on(tool: str, *constraints: str) -> dict:
    return {"tools": {tool: {"mode": "allow", "constraints": list(constraints)}}}


# load_known_names


def test_load_known_names_happy_path(tmp_path) -> None:
    # `region` is only in ops-bot's audit rows, `wire_transfer` only in its manifest.
    assert load_known_names(make_workspace(tmp_path), AGENT) == (TOOLS, {"department"})


def test_when_audit_json_is_the_endpoints_page_then_its_rows_are_read(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    page = {"rows": AUDIT, "total": len(AUDIT), "limit": 25, "offset": 0}
    (ws / "audit.json").write_text(json.dumps(page))
    assert load_known_names(ws, AGENT)[1] == {"department"}


def test_when_audit_json_is_missing_then_no_attribute_is_known(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    (ws / "audit.json").unlink()
    assert load_known_names(ws, AGENT) == (TOOLS, set())


def test_when_no_agent_is_given_then_every_agents_names_are_known(tmp_path) -> None:
    tools, attrs = load_known_names(make_workspace(tmp_path), None)
    assert tools == {**TOOLS, "wire_transfer": {"iban"}}
    assert attrs == {"department", "region"}


def test_when_the_case_agent_has_no_manifest_then_loading_fails(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    with pytest.raises(ValueError, match="billing-bot"):
        load_known_names(ws, "billing-bot")


# unknown_tools


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
def test_unknown_tools_flags_a_tool_not_in_the_manifest(tool) -> None:
    doc = {"roles": {"billing": on("refund_order"), "ops": on(tool)}}
    assert unknown_tools(doc, TOOLS) == [tool]


def test_unknown_tools_accepts_synthetic_and_agent_keys() -> None:
    keys = ["net.http_request", "net.tcp_connect", "agent.run", "agent.tool:b"]
    doc = {"tools": {k: {"mode": "allow"} for k in keys}}
    assert unknown_tools(doc, TOOLS) == []


# unknown_refs


def test_unknown_refs_flags_an_argument_missing_from_the_manifest() -> None:
    doc = on("refund_order", 'args.tier != "gold" or args.amount <= 1000')
    assert unknown_refs(doc, TOOLS, set()) == ["refund_order: args.tier"]


@pytest.mark.parametrize(
    ("attribute", "known"), [("department", True), ("team", False)]
)
def test_unknown_refs_checks_caller_attributes(attribute, known) -> None:
    refs = unknown_refs(
        on("refund_order", f'ctx.{attribute} == "x"'), TOOLS, {"department"}
    )
    assert refs == ([] if known else [f"refund_order: ctx.{attribute}"])


@pytest.mark.parametrize(
    "constraint",
    [
        'user.department == "finance"',
        "arg.amount <= 5",
        "attrs.vip == true",
        "run.tool_cals < 20",
        "run.tool_calls.x < 20",
        'role.name == "x"',
    ],
)
def test_unknown_refs_flags_a_path_with_no_such_root(constraint) -> None:
    [ref] = unknown_refs(on("refund_order", constraint), TOOLS, {"department"})
    assert ref == f"refund_order: {constraint.split()[0]}"


def test_unknown_refs_accepts_known_run_paths_and_bare_facts() -> None:
    constraint = 'run.tool_calls < 20 and role == "billing" and tool == "refund_order"'
    assert unknown_refs(on("refund_order", constraint), TOOLS, set()) == []


def test_unknown_refs_reads_paths_not_string_literals() -> None:
    # `ctx.x` inside a string is data, and quantifier bodies are walked.
    constraint = 'args.note == "see ctx.x" and any(args.items, .price < 5)'
    tools = {"refund_order": {"note", "items"}}
    assert unknown_refs(on("refund_order", constraint), tools, set()) == []


def test_unknown_refs_scans_skill_constraints() -> None:
    skill = {"mode": "allow", "constraints": ["ctx.invented == 1"]}
    doc = {"roles": {"billing": {"skills": {"pdf": skill}}}}
    assert unknown_refs(doc, {}, {"department"}) == ["skill:pdf: ctx.invented"]


def test_unknown_refs_checks_skill_constraints_against_the_skill_args() -> None:
    constraints = [
        'startswith(args.file_path, "scripts/") and args.content_hash != ""',
        "args.amount <= 500",  # a tool argument, never on a skill call
    ]
    doc = {"skills": {"pdf": {"mode": "allow", "constraints": constraints}}}
    assert unknown_refs(doc, TOOLS, set()) == ["skill:pdf: args.amount"]


@pytest.mark.parametrize(
    "doc",
    [
        {"constraints": ['ctx.departmnt == "x"']},  # a flat file's own fence
        {"constraints": ['ctx.departmnt == "x"'], "roles": {"billing": {}}},
        {"roles": {"billing": {"constraints": ['ctx.departmnt == "x"']}}},
    ],
)
def test_unknown_refs_scans_file_and_role_constraints(doc) -> None:
    assert unknown_refs(doc, {}, {"department"}) == ["policy-level: ctx.departmnt"]


def test_a_policy_level_constraint_may_read_the_skill_args() -> None:
    doc = {
        "constraints": ['args.skill != "shell"'],
        "skills": {"pdf": {"mode": "allow"}, "shell": {"mode": "allow"}},
    }
    assert unknown_refs(doc, {}, set()) == []


def test_when_a_synthetic_key_reads_its_gate_args_then_unknown_refs_checks_each() -> (
    None
):
    doc = {
        "roles": {
            "a": on("net.http_request", 'args.method == "GET"'),
            "b": on("agent.tool:billing", 'args.via == "tool"'),
            "c": on("agent.run", 'args.target == "billing"'),
            "d": on("agent.handoff:billing", "args.amount <= 500"),
        }
    }
    assert unknown_refs(doc, {}, set()) == [
        "agent.handoff:billing: args.amount",
        "agent.run: args.target",
    ]


def test_when_a_default_admission_or_reach_block_has_a_typo_then_unknown_refs_flags_it() -> (
    None
):
    doc = {
        "default_policy": {"mode": "allow", "constraints": ['ctx.departmnt == "x"']},
        "admission": {"mode": "allow", "constraints": ['args.agnt == "x"']},
        "agents": {"billing": {"mode": "allow", "constraints": ['args.trgt == "x"']}},
    }
    assert unknown_refs(doc, {}, {"department"}) == [
        "agent.run: args.agnt",
        "agent.tool:billing: args.trgt",
        "policy-level: ctx.departmnt",
    ]


# name_checks


def test_name_checks_happy_path(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    policy, _ = effective_policy(ws)
    before = snapshot(ws)
    assert all(c.passed for c in name_checks(policy, ws, AGENT, before, before))


@pytest.mark.parametrize("broken", ["", "{not json", "[]"])
def test_when_the_agent_breaks_agents_json_then_both_name_checks_fail(
    tmp_path, broken
) -> None:
    ws = make_workspace(tmp_path)
    policy, _ = effective_policy(ws)
    (ws / "agents.json").write_text(broken)
    after = snapshot(ws)
    assert not any(c.passed for c in name_checks(policy, ws, AGENT, after, after))


def test_when_the_agent_edits_agents_json_then_both_name_checks_fail(tmp_path) -> None:
    ws = make_workspace(tmp_path, POLICY + "      wire_transfer: { mode: allow }\n")
    policy, _ = effective_policy(ws)
    before = snapshot(ws)
    # The agent "fixes" its invented tool by adding it to the manifest.
    agents = json.loads((ws / "agents.json").read_text())
    agents[0]["manifest"]["tools"].append(manifest_tool("wire_transfer", iban="string"))
    (ws / "agents.json").write_text(json.dumps(agents))
    checks = by_name(name_checks(policy, ws, AGENT, before, snapshot(ws)))
    assert checks["only known tools"].detail == "edited during the run: ['agents.json']"
    assert not checks["only known tools"].passed
    assert not checks["only known arguments and attributes"].passed


def test_when_the_agent_edits_audit_json_then_both_name_checks_fail(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    policy, _ = effective_policy(ws)
    before = snapshot(ws)
    # The agent "fixes" an invented attribute by adding an audit row with it.
    row = {"agent_name": AGENT, "tool_name": "view_orders", "attributes": {"tier": 1}}
    (ws / "audit.json").write_text(json.dumps([*AUDIT, row]))
    checks = name_checks(policy, ws, AGENT, before, snapshot(ws))
    assert [c.detail for c in checks] == ["edited during the run: ['audit.json']"] * 2


def test_when_the_case_names_no_agent_then_name_checks_accept_every_agents_names(
    tmp_path,
) -> None:
    # A role or project-wide edit: ops-bot's tool and attribute are fine too.
    policy = POLICY.replace("- args.amount <= 500", '- ctx.region == "eu"')
    ws = make_workspace(tmp_path, policy + "      wire_transfer: { mode: allow }\n")
    files = snapshot(ws)
    assert all(
        c.passed for c in name_checks(effective_policy(ws)[0], ws, None, files, files)
    )
    (ws / "policy.yaml").write_text(policy + "      teleport: { mode: allow }\n")
    files = snapshot(ws)
    checks = by_name(name_checks(effective_policy(ws)[0], ws, None, files, files))
    assert (
        checks["only known tools"].detail == "not in any agent's manifest: ['teleport']"
    )


# invented_names on a module tree: each file against the agents it reaches


def test_invented_names_happy_path_on_a_module_tree(tmp_path) -> None:
    roles = '  billing:\n    "*": [read_only]\n    ops-bot: [read_only, ops]\n'
    ws = make_modules_workspace(tmp_path, roles)
    # In ops-bot's column only, with ops-bot's tool and attribute.
    write_module(
        ws,
        "capabilities/ops.yaml",
        "tools:\n  wire_transfer: { mode: allow, constraints: ['ctx.region == \"eu\"'] }\n",
    )
    # Org-wide: a boundary may deny another agent's tool, on its attribute.
    add_boundary_tool(
        ws, "wire_transfer: { mode: deny, constraints: ['ctx.region == \"us\"'] }"
    )
    assert invented_names(ws, {}, AGENT) == ([], [])


def test_when_a_module_file_no_column_imports_invents_names_then_they_are_unknown(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path, "  billing: [read_only]\n")
    write_module(
        ws,
        "capabilities/extra.yaml",
        "tools:\n  teleport: { mode: allow, constraints: ['ctx.nope == 1'] }\n",
    )
    add_boundary_tool(ws, "warp: { mode: deny }")
    assert invented_names(ws, {}, AGENT) == (
        [
            "policies/boundaries/org.yaml: warp (no agent has it)",
            "policies/capabilities/extra.yaml: teleport (no agent has it)",
        ],
        ["policies/capabilities/extra.yaml: teleport: ctx.nope"],
    )


@pytest.mark.parametrize(
    "roles",
    [
        "  billing: [read_only, payments]\n",
        # Only shop-bot's own cell imports it, not "*".
        '  billing:\n    "*": [read_only]\n    shop-bot: [read_only, payments]\n',
    ],
)
def test_when_the_agents_column_grants_another_agents_tool_then_it_is_unknown(
    tmp_path, roles
) -> None:
    ws = make_modules_workspace(tmp_path, roles)
    write_module(
        ws,
        "capabilities/payments.yaml",
        "tools:\n  refund_order: { mode: allow }\n  wire_transfer: { mode: allow }\n",
    )
    tools, _ = invented_names(ws, {}, AGENT)
    assert tools == ["policies/capabilities/payments.yaml: wire_transfer"]
    # A role or project-wide case accepts any agent's tool.
    assert invented_names(ws, {}, None) == ([], [])


def test_when_the_agents_column_reads_another_agents_attribute_then_it_is_unknown(
    tmp_path,
) -> None:
    ws = make_modules_workspace(tmp_path, "  billing: [read_only, payments]\n")
    write_module(
        ws,
        "capabilities/payments.yaml",
        "tools:\n  refund_order: { mode: allow, constraints: ['ctx.region == \"eu\"'] }\n",
    )
    _, refs = invented_names(ws, {}, AGENT)
    assert refs == ["policies/capabilities/payments.yaml: refund_order: ctx.region"]


# The synthetic argument sets against the gates that build them


def _recording_enforcer(policy: dict, seen: list):
    return build_enforcer(
        load_policy_set_from_dict(policy),
        agent_name="a",
        decision_observer=seen.append,
    )


def test_http_request_args_match_the_egress_proxy() -> None:
    built = (
        connect_to_args("example.com", 443).keys()
        | http_to_args("GET", "http://example.com/p?q=1").keys()
    )
    assert built == SYNTHETIC_ARGS["net.http_request"]


async def test_tcp_connect_args_match_the_tcp_proxy() -> None:
    seen: list = []
    upstream = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    target = ("127.0.0.1", upstream.sockets[0].getsockname()[1])
    enforcer = _recording_enforcer(
        {"tools": {"net.tcp_connect": {"mode": "deny"}}}, seen
    )
    proxy = TcpEgressProxy(enforcer, HexgateContext(user_id="u"), target=target)
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        await reader.read()  # the deny closes the connection
        writer.close()
    finally:
        await proxy.stop()
        upstream.close()
    assert seen[0].arguments.keys() == SYNTHETIC_ARGS["net.tcp_connect"]


def test_agent_args_match_the_agent_gates() -> None:
    seen: list = []
    enforcer = _recording_enforcer(
        {"admission": {"mode": "allow"}, "agents": {"b": {"mode": "allow"}}}, seen
    )
    with HexgateContext(user_id="u").sync_scope():
        resolve_agent_gate(enforcer).check_admission()
        resolve_reach_gate(enforcer).check_reach("b", via="tool")
    assert seen[0].arguments.keys() == SYNTHETIC_ARGS["agent.run"]
    assert seen[1].arguments.keys() == AGENT_REACH_ARGS


def test_skill_args_match_the_skill_seams() -> None:
    _, script = _skill_decision(object(), "script", {"skill_name": "pdf"})
    location = SkillLocation("pdf", "/skills/pdf/SKILL.md", None)
    read = _SkillRead("resource", location, "/skills/pdf/a.md").override(None)
    assert script.keys() == SKILL_ARGS
    assert read.args.keys() <= SKILL_ARGS
