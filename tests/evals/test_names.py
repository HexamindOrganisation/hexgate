"""Known names and invented ones (`names.py`), on synthetic workspaces."""

from __future__ import annotations

import asyncio
import json

import pytest

from evals.policy_writing.checks import score, snapshot
from evals.policy_writing.names import (
    AGENT_REACH_ARGS,
    SYNTHETIC_ARGS,
    load_known_names,
    unknown_refs,
)
from evals.policy_writing.policy import effective_policy
from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.egress.tcp import TcpEgressProxy
from hexgate.runtime.context import HexgateContext
from hexgate.security import resolve_agent_gate, resolve_reach_gate
from hexgate.security.enforcer import build_enforcer
from hexgate.security.policy_set import load_policy_set_from_dict
from tests.evals.workspace import (
    AGENT,
    AUDIT,
    POLICY,
    by_name,
    make_workspace,
    manifest_tool,
)


def test_unknown_refs_flags_an_argument_missing_from_the_manifest(tmp_path) -> None:
    policy = POLICY.replace(
        "- args.amount <= 500", '- args.tier != "gold" or args.amount <= 1000'
    )
    ws = make_workspace(tmp_path, policy)
    tools, attrs = load_known_names(ws, AGENT)
    policy, _ = effective_policy(ws)
    assert unknown_refs(policy.payload, tools, attrs) == ["refund_order: args.tier"]


@pytest.mark.parametrize(
    ("attribute", "known"),
    [
        ("department", True),  # in shop-bot's audit rows
        ("team", False),  # in no audit row
        ("region", False),  # only in ops-bot's audit rows
    ],
)
def test_caller_attributes_come_from_the_agents_audit_rows(
    tmp_path, attribute, known
) -> None:
    policy = POLICY.replace("- args.amount <= 500", f'- ctx.{attribute} == "x"')
    ws = make_workspace(tmp_path, policy)
    tools, attrs = load_known_names(ws, AGENT)
    policy, _ = effective_policy(ws)
    refs = unknown_refs(policy.payload, tools, attrs)
    assert refs == ([] if known else [f"refund_order: ctx.{attribute}"])


def test_when_audit_json_is_the_endpoints_page_then_its_rows_are_read(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    page = {"rows": AUDIT, "total": len(AUDIT), "limit": 25, "offset": 0}
    (ws / "audit.json").write_text(json.dumps(page))
    assert load_known_names(ws, AGENT)[1] == {"department"}


def test_when_an_egress_key_is_misspelled_then_it_is_an_unknown_tool(tmp_path) -> None:
    policy = POLICY + "      net.http_reqest: { mode: allow }\n"
    ws = make_workspace(tmp_path, policy)
    check = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["only known tools"]
    assert check.detail == "not in shop-bot's manifest: ['net.http_reqest']"


def test_when_audit_json_is_missing_then_no_attribute_is_known(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    (ws / "audit.json").unlink()
    tools, attrs = load_known_names(ws, AGENT)
    assert set(tools) == {"view_orders", "refund_order"}
    assert attrs == set()


@pytest.mark.parametrize("broken", ["", "{not json", "[]"])
def test_when_the_agent_breaks_agents_json_then_both_name_checks_fail(
    tmp_path, broken
) -> None:
    ws = make_workspace(tmp_path)
    (ws / "agents.json").write_text(broken)
    checks = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))
    assert not checks["only known tools"].passed
    assert not checks["only known arguments and attributes"].passed


def test_when_the_agent_edits_agents_json_then_both_name_checks_fail(tmp_path) -> None:
    policy = POLICY + "      wire_transfer: { mode: allow }\n"
    ws = make_workspace(tmp_path, policy)
    before = snapshot(ws)
    # The agent "fixes" its invented tool by adding it to the manifest.
    agents = json.loads((ws / "agents.json").read_text())
    agents[0]["manifest"]["tools"].append(manifest_tool("wire_transfer", iban="string"))
    (ws / "agents.json").write_text(json.dumps(agents))
    checks = by_name(score({"agent": AGENT}, ws, before, ""))
    assert checks["only known tools"].detail == "edited during the run: ['agents.json']"
    assert not checks["only known tools"].passed
    assert not checks["only known arguments and attributes"].passed


def test_when_the_case_agent_has_no_manifest_then_loading_fails(tmp_path) -> None:
    ws = make_workspace(tmp_path)
    with pytest.raises(ValueError, match="billing-bot"):
        load_known_names(ws, "billing-bot")


@pytest.mark.parametrize(
    "constraint",
    [
        'user.department == "finance"',
        "arg.amount <= 5",
        "attrs.vip == true",
        "run.tool_cals < 20",
        'role.name == "x"',
    ],
)
def test_unknown_refs_flags_a_path_with_no_such_root(constraint) -> None:
    doc = {"tools": {"refund_order": {"mode": "allow", "constraints": [constraint]}}}
    tools = {"refund_order": {"amount"}}
    [ref] = unknown_refs(doc, tools, {"department"})
    assert ref == f"refund_order: {constraint.split()[0]}"


def test_unknown_refs_accepts_known_run_paths_and_bare_facts() -> None:
    constraint = 'run.tool_calls < 20 and role == "billing" and tool == "refund_order"'
    doc = {"tools": {"refund_order": {"mode": "allow", "constraints": [constraint]}}}
    assert unknown_refs(doc, {"refund_order": set()}, set()) == []


def test_unknown_refs_reads_paths_not_string_literals() -> None:
    # `ctx.x` inside a string is data, and quantifier bodies are walked.
    constraint = 'args.note == "see ctx.x" and any(args.items, .price < 5)'
    doc = {"tools": {"refund_order": {"mode": "allow", "constraints": [constraint]}}}
    tools = {"refund_order": {"note", "items"}}
    assert unknown_refs(doc, tools, set()) == []


def test_unknown_refs_scans_skill_constraints() -> None:
    skill = {"mode": "allow", "constraints": ["ctx.invented == 1"]}
    doc = {"roles": {"billing": {"skills": {"pdf": skill}}}}
    assert unknown_refs(doc, {}, {"department"}) == ["policy-level: ctx.invented"]


def test_when_a_synthetic_key_reads_its_gate_args_then_unknown_refs_checks_each() -> (
    None
):
    def on(tool: str, constraint: str) -> dict:
        return {"tools": {tool: {"mode": "allow", "constraints": [constraint]}}}

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


def test_when_a_tool_is_only_in_another_agents_manifest_then_it_is_unknown(
    tmp_path,
) -> None:
    policy = POLICY + "      wire_transfer: { mode: allow }\n"
    ws = make_workspace(tmp_path, policy)
    check = by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["only known tools"]
    assert not check.passed
    assert check.detail == "not in shop-bot's manifest: ['wire_transfer']"
