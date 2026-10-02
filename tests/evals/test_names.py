"""Known names and invented ones (`names.py`), and the synthetic argument sets
against the gates that build them."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from evals.policy_writing.names import (
    AGENT_REACH_ARGS,
    SKILL_ARGS,
    SKILL_SCRIPT_ARGS,
    SYNTHETIC_ARGS,
    KnownNames,
    load_known_names,
    unknown_keys,
    unknown_refs,
)
from hexgate.adapters.google.tools import _skill_decision
from hexgate.adapters.langchain.skills import _SkillRead
from hexgate.egress.tcp import TcpEgressProxy
from hexgate.manifest.langchain import SkillLocation
from hexgate.runtime.context import HexgateContext
from hexgate.security import resolve_agent_gate, resolve_reach_gate
from hexgate.security.enforcer import build_enforcer
from hexgate.security.policy_set import load_policy_set_from_dict
from tests.evals.helpers import (
    AGENT,
    AUDIT,
    agent_view,
    make_workspace,
)

# shop-bot's manifest, as `load_known_names` reads it from the fixture.
TOOLS = {"view_orders": {"customer_id"}, "refund_order": {"order_id", "amount"}}
KNOWN = KnownNames(TOOLS, {"department"}, {"pdf"}, {"redact_pii"})
ALLOW = {"mode": "allow"}


def loaded(doc: dict):
    return load_policy_set_from_dict({"version": 1, **doc})


def on(tool: str, *constraints: str) -> dict:
    return {"tools": {tool: {"mode": "allow", "constraints": list(constraints)}}}


# load_known_names


def test_load_known_names_happy_path(tmp_path) -> None:
    # `region`, `wire_transfer` and `ledger` are only ops-bot's; draft-bot has no
    # manifest yet.
    assert load_known_names(make_workspace(tmp_path), AGENT) == KNOWN


def test_when_audit_json_is_the_endpoints_page_then_its_rows_are_read(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    page = {"rows": AUDIT, "total": len(AUDIT), "limit": 25, "offset": 0}
    (ws / "audit.json").write_text(json.dumps(page))
    assert load_known_names(ws, AGENT).attrs == {"department"}


def test_when_audit_json_is_missing_then_no_attribute_is_known(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    (ws / "audit.json").unlink()
    assert load_known_names(ws, AGENT).attrs == set()


def test_when_the_manifest_lists_no_skills_or_guards_then_none_are_known(
    tmp_path,
) -> None:
    ws = make_workspace(tmp_path)
    (ws / "agents.json").write_text(json.dumps([agent_view(AGENT)]))
    known = load_known_names(ws, AGENT)
    assert (known.skills, known.guards) == (set(), set())


@pytest.mark.parametrize("agent", ["billing-bot", "draft-bot"])  # absent, no manifest
def test_when_the_case_agent_has_no_manifest_then_loading_fails(
    tmp_path, agent
) -> None:
    ws = make_workspace(tmp_path)
    with pytest.raises(ValueError, match=f"has no manifest for agent '{agent}'"):
        load_known_names(ws, agent)


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
            "billing": fenced('args.via == "tool"', via=["tool"]),
            "ops-bot": fenced("args.amount <= 500", via=["handoff"]),
        },
    }
    assert unknown_refs(loaded(doc), KNOWN) == [
        "agent.handoff:ops-bot: args.amount",
        "agent.run: args.target",
    ]


# The synthetic argument sets against the gates that build them


def _recording_enforcer(policy: dict, seen: list):
    return build_enforcer(
        load_policy_set_from_dict(policy),
        agent_name="a",
        decision_observer=seen.append,
    )


async def test_when_the_tcp_proxy_decides_then_its_args_match_tcp_connect() -> None:
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


def test_when_the_agent_gates_decide_then_their_args_match() -> None:
    seen: list = []
    enforcer = _recording_enforcer(
        {"admission": {"mode": "allow"}, "agents": {"b": {"mode": "allow"}}}, seen
    )
    with HexgateContext(user_id="u").sync_scope():
        resolve_agent_gate(enforcer).check_admission()
        resolve_reach_gate(enforcer).check_reach("b", via="tool")
    assert seen[0].arguments.keys() == SYNTHETIC_ARGS["agent.run"]
    assert seen[1].arguments.keys() == AGENT_REACH_ARGS


def test_when_the_skill_seams_decide_then_their_args_match() -> None:
    _, script = _skill_decision(object(), "script", {"skill_name": "pdf"})
    _, instructions = _skill_decision(object(), "instructions", {"skill_name": "pdf"})
    location = SkillLocation("pdf", "/skills/pdf/SKILL.md", None)
    read = _SkillRead("resource", location, "/skills/pdf/a.md").override(None)
    assert script.keys() == SKILL_SCRIPT_ARGS
    assert instructions.keys() == read.args.keys() == SKILL_ARGS
