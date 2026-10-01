"""The policy-writing scorer, on small synthetic workspaces (no LLM, no case data)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evals.policy_writing.checks import (
    AGENT_REACH_ARGS,
    SYNTHETIC_ARGS,
    decide,
    effective_policy,
    load_known_names,
    score,
    snapshot,
    unknown_refs,
)
from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.egress.tcp import TcpEgressProxy
from hexgate.runtime.context import HexgateContext
from hexgate.security import resolve_agent_gate, resolve_reach_gate
from hexgate.security.enforcer import build_enforcer
from hexgate.security.policy_set import load_policy_set_from_dict

AGENT = "shop-bot"


def _tool(name: str, **args: str) -> dict:
    return {
        "name": name,
        "description": name,
        "input_schema": {
            "properties": {a: {"title": a, "type": t} for a, t in args.items()},
            "required": list(args),
        },
    }


def _view(name: str, *tools: dict) -> dict:
    # One `GET /agents/manifest` entry (AgentManifestView), as `agents_list` returns it.
    manifest = {"name": name, "framework": "langchain", "tools": list(tools)}
    return {
        "name": name,
        "manifest": manifest,
        "version": 1,
        "content_hash": "h",
        "updated_at": "2026-10-01T00:00:00Z",
    }


AGENTS = [
    _view(
        AGENT,
        _tool("view_orders", customer_id="string"),
        _tool("refund_order", order_id="string", amount="number"),
    ),
    # Another agent in the project: its tools and attributes are not shop-bot's.
    _view("ops-bot", _tool("wire_transfer", iban="string")),
]

# `audit_decisions` rows (AuditDecisionRow fields); only their attributes matter here.
AUDIT = [
    {
        "agent_name": AGENT,
        "tool_name": "view_orders",
        "attributes": {"department": "x"},
    },
    {"agent_name": AGENT, "tool_name": "view_orders", "attributes": None},
    {
        "agent_name": "ops-bot",
        "tool_name": "wire_transfer",
        "attributes": {"region": "eu"},
    },
]

POLICY = """\
version: 1
roles:
  default:
    tools:
      view_orders: { mode: allow }
  support:
    tools:
      view_orders: { mode: allow }
      refund_order:
        mode: approval_required
  billing:
    tools:
      view_orders: { mode: allow }
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 500
"""


def _project_files(ws: Path) -> None:
    (ws / "agents.json").write_text(json.dumps(AGENTS))
    (ws / "audit.json").write_text(json.dumps(AUDIT))


def _workspace(tmp_path: Path, policy: str = POLICY) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _project_files(ws)
    (ws / "policy.yaml").write_text(policy)
    return ws


def _by_name(checks) -> dict:
    return {c.name: c for c in checks}


@pytest.mark.parametrize(
    ("role", "tool", "args", "outcome"),
    [
        ("billing", "refund_order", {"order_id": "o1", "amount": 500}, "allow"),
        ("billing", "refund_order", {"order_id": "o1", "amount": 501}, "deny"),
        (
            "support",
            "refund_order",
            {"order_id": "o1", "amount": 10},
            "approval_required",
        ),
    ],
)
def test_decide_reports_each_outcome(tmp_path, role, tool, args, outcome) -> None:
    ws = _workspace(tmp_path)
    policy, _ = effective_policy(ws)
    got, raw = decide(policy, role, {"tool": tool, "args": args})
    assert got == outcome, raw


def test_decide_reads_the_outcome_not_the_arguments(tmp_path) -> None:
    # The outcome comes from the verdict, so an argument that spells an outcome
    # can't be mistaken for one.
    ws = _workspace(tmp_path)
    policy, _ = effective_policy(ws)
    d = {"tool": "refund_order", "args": {"order_id": "ALLOW", "amount": 900}}
    got, reason = decide(policy, "billing", d)
    assert got == "deny"
    assert "amount" in reason


def test_decide_rejects_an_undefined_role(tmp_path) -> None:
    # Not the `default` fallback: a case naming a role the policy lacks fails.
    ws = _workspace(tmp_path)
    policy, _ = effective_policy(ws)
    got, reason = decide(policy, "suport", {"tool": "view_orders"})
    assert got == "error"
    assert "suport" in reason


def test_decision_checks_pass_and_fail(tmp_path) -> None:
    ws = _workspace(tmp_path)
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


def test_unknown_refs_flags_an_argument_missing_from_the_manifest(tmp_path) -> None:
    policy = POLICY.replace(
        "- args.amount <= 500", '- args.tier != "gold" or args.amount <= 1000'
    )
    ws = _workspace(tmp_path, policy)
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
    ws = _workspace(tmp_path, policy)
    tools, attrs = load_known_names(ws, AGENT)
    policy, _ = effective_policy(ws)
    refs = unknown_refs(policy.payload, tools, attrs)
    assert refs == ([] if known else [f"refund_order: ctx.{attribute}"])


def test_when_audit_json_is_the_endpoints_page_then_its_rows_are_read(tmp_path) -> None:
    ws = _workspace(tmp_path)
    page = {"rows": AUDIT, "total": len(AUDIT), "limit": 25, "offset": 0}
    (ws / "audit.json").write_text(json.dumps(page))
    assert load_known_names(ws, AGENT)[1] == {"department"}


def test_when_an_egress_key_is_misspelled_then_it_is_an_unknown_tool(tmp_path) -> None:
    policy = POLICY + "      net.http_reqest: { mode: allow }\n"
    ws = _workspace(tmp_path, policy)
    check = _by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["only known tools"]
    assert check.detail == "not in shop-bot's manifest: ['net.http_reqest']"


def test_when_audit_json_is_missing_then_no_attribute_is_known(tmp_path) -> None:
    ws = _workspace(tmp_path)
    (ws / "audit.json").unlink()
    tools, attrs = load_known_names(ws, AGENT)
    assert set(tools) == {"view_orders", "refund_order"}
    assert attrs == set()


@pytest.mark.parametrize("broken", ["", "{not json", "[]"])
def test_when_the_agent_breaks_agents_json_then_both_name_checks_fail(
    tmp_path, broken
) -> None:
    ws = _workspace(tmp_path)
    (ws / "agents.json").write_text(broken)
    checks = _by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))
    assert not checks["only known tools"].passed
    assert not checks["only known arguments and attributes"].passed


def test_when_the_agent_edits_agents_json_then_both_name_checks_fail(tmp_path) -> None:
    policy = POLICY + "      wire_transfer: { mode: allow }\n"
    ws = _workspace(tmp_path, policy)
    before = snapshot(ws)
    # The agent "fixes" its invented tool by adding it to the manifest.
    agents = json.loads((ws / "agents.json").read_text())
    agents[0]["manifest"]["tools"].append(_tool("wire_transfer", iban="string"))
    (ws / "agents.json").write_text(json.dumps(agents))
    checks = _by_name(score({"agent": AGENT}, ws, before, ""))
    assert checks["only known tools"].detail == "edited during the run: ['agents.json']"
    assert not checks["only known tools"].passed
    assert not checks["only known arguments and attributes"].passed


def test_when_the_case_agent_has_no_manifest_then_loading_fails(tmp_path) -> None:
    ws = _workspace(tmp_path)
    with pytest.raises(ValueError, match="billing-bot"):
        load_known_names(ws, "billing-bot")


@pytest.mark.parametrize(
    "constraint",
    ['user.department == "finance"', "arg.amount <= 5", "attrs.vip == true"],
)
def test_unknown_refs_flags_a_path_with_no_such_root(constraint) -> None:
    doc = {"tools": {"refund_order": {"mode": "allow", "constraints": [constraint]}}}
    tools = {"refund_order": {"amount"}}
    [ref] = unknown_refs(doc, tools, {"department"})
    assert ref == f"refund_order: {constraint.split()[0]}"


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


# The scorer's copy of each synthetic key's arguments must match what the gate
# that makes the call actually sends, or a valid `args.x` gets flagged.


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


def test_snapshot_ignores_every_path_under_a_dot_directory(tmp_path) -> None:
    ws = _workspace(tmp_path)
    skill = ws / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("installed by the harness")
    (ws / ".effective.yaml").write_text("{}")
    assert set(snapshot(ws)) == {"agents.json", "audit.json", "policy.yaml"}


def test_no_changes_ignores_an_installed_skill(tmp_path) -> None:
    ws = _workspace(tmp_path)
    before = snapshot(ws)
    skill = ws / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("installed by the harness")
    checks = _by_name(
        score({"agent": AGENT, "expect": {"no_changes": True}}, ws, before, "")
    )
    assert checks["no changes"].passed


def test_no_changes_fails_on_one_changed_file(tmp_path) -> None:
    ws = _workspace(tmp_path)
    before = snapshot(ws)
    (ws / "audit.json").write_text("[]")
    checks = _by_name(
        score({"agent": AGENT, "expect": {"no_changes": True}}, ws, before, "")
    )
    assert not checks["no changes"].passed
    assert checks["no changes"].detail == "changed: ['audit.json']"


def test_changed_and_unchanged(tmp_path) -> None:
    ws = _workspace(tmp_path)
    before = snapshot(ws)
    (ws / "policy.yaml").write_text(POLICY + "# edited\n")
    case = {
        "agent": AGENT,
        "expect": {"changed": ["policy.yaml"], "unchanged": ["agents.json"]},
    }
    checks = _by_name(score(case, ws, before, ""))
    assert checks["changed: policy.yaml"].passed
    assert checks["unchanged: agents.json"].passed

    case = {
        "agent": AGENT,
        "expect": {"changed": ["agents.json"], "unchanged": ["policy.yaml"]},
    }
    checks = _by_name(score(case, ws, before, ""))
    assert not checks["changed: agents.json"].passed
    assert not checks["unchanged: policy.yaml"].passed


def test_superset_reports_the_violating_probe(tmp_path) -> None:
    ws = _workspace(tmp_path)
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
    check = _by_name(score(case, ws, snapshot(ws), ""))["superset: support ⊇ billing"]
    assert not check.passed
    assert check.detail == "refund_order: billing=allow, support=approval_required"


def test_when_the_narrower_role_is_missing_then_superset_fails(tmp_path) -> None:
    ws = _workspace(tmp_path)
    probe = {"tool": "view_orders", "args": {"customer_id": "c1"}}
    case = {
        "agent": AGENT,
        "expect": {
            "superset": [{"wider": "support", "narrower": "nosuch", "probes": [probe]}]
        },
    }
    check = _by_name(score(case, ws, snapshot(ws), ""))["superset: support ⊇ nosuch"]
    assert not check.passed
    assert check.detail == "view_orders: nosuch=error, support=allow"


def test_mentions_are_case_insensitive(tmp_path) -> None:
    ws = _workspace(tmp_path)
    case = {
        "agent": AGENT,
        "expect": {
            "mentions_any": ["Boundary", "ceiling"],
            "mentions_all": ["REFUND", "Security"],
        },
    }
    answer = "The BOUNDARY caps refunds; ask security."
    checks = _by_name(score(case, ws, snapshot(ws), answer))
    assert checks["answer mentions one of"].passed
    assert checks["answer mentions all of"].passed

    checks = _by_name(score(case, ws, snapshot(ws), "done"))
    assert not checks["answer mentions one of"].passed
    assert checks["answer mentions all of"].detail == "missing ['REFUND', 'Security']"


def test_when_a_tool_is_only_in_another_agents_manifest_then_it_is_unknown(
    tmp_path,
) -> None:
    policy = POLICY + "      wire_transfer: { mode: allow }\n"
    ws = _workspace(tmp_path, policy)
    check = _by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["only known tools"]
    assert not check.passed
    assert check.detail == "not in shop-bot's manifest: ['wire_transfer']"


def test_clean_policy_is_valid(tmp_path) -> None:
    ws = _workspace(tmp_path)
    assert _by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["valid"].passed


def test_lint_warning_fails_valid(tmp_path) -> None:
    # Parses and builds, but `default` grants a tool no named role grants: a
    # warning at the CLI's default threshold, a failure at ours.
    policy = """\
version: 1
roles:
  default:
    tools:
      refund_order: { mode: allow }
  billing:
    tools:
      view_orders: { mode: allow }
"""
    ws = _workspace(tmp_path, policy)
    case = {
        "agent": AGENT,
        "expect": {
            "decisions": [{"role": "billing", "tool": "view_orders", "expect": "allow"}]
        },
    }
    checks = score(case, ws, snapshot(ws), "")
    valid = _by_name(checks)["valid"]
    assert not valid.passed
    assert "permissive-default" in valid.detail
    # An invalid policy fails its decisions rather than skipping them.
    assert [c.detail for c in checks if c.name.startswith("decision:")] == [
        "policy invalid"
    ]


def _modules_workspace(tmp_path: Path, roles: str) -> Path:
    ws = tmp_path / "ws"
    (ws / "policies" / "boundaries").mkdir(parents=True)
    (ws / "policies" / "capabilities").mkdir()
    _project_files(ws)
    (ws / "policies" / "boundaries" / "org.yaml").write_text(
        "default_policy: { mode: allow }\n"
        "tools:\n"
        '  refund_order: { mode: allow, constraints: ["args.amount <= 1000"] }\n'
    )
    (ws / "policies" / "capabilities" / "read_only.yaml").write_text(
        "tools:\n  view_orders: { mode: allow }\n"
    )
    (ws / "policies" / "capabilities" / "payments.yaml").write_text(
        "tools:\n  refund_order: { mode: allow }\n"
    )
    (ws / "roles.yaml").write_text(f"version: 1\nroles:\n{roles}")
    return ws


def test_modules_layout_is_valid(tmp_path) -> None:
    ws = _modules_workspace(
        tmp_path, "  default: [read_only]\n  billing: [read_only, payments]\n"
    )
    case = {
        "agent": AGENT,
        "expect": {
            "decisions": [
                {
                    "role": "billing",
                    "tool": "refund_order",
                    "args": {"amount": 1001},
                    "expect": "deny",
                }
            ]
        },
    }
    checks = score(case, ws, snapshot(ws), "")
    assert all(c.passed for c in checks), checks


def test_modules_layout_resolves_the_case_agents_column(tmp_path) -> None:
    roles = '  billing:\n    "*": [read_only]\n    shop-bot: [read_only, payments]\n'
    ws = _modules_workspace(tmp_path, roles)
    refund = {"role": "billing", "tool": "refund_order", "args": {"amount": 5}}
    for agent, outcome in [(AGENT, "allow"), ("ops-bot", "deny")]:
        policy, _ = effective_policy(ws, agent)
        assert decide(policy, "billing", refund)[0] == outcome


def test_modules_layout_fails_valid_on_a_permissive_default(tmp_path) -> None:
    # `policy check` doesn't lint the composed roles; only the resolved policy
    # shows that `default` grants what no named role does.
    ws = _modules_workspace(
        tmp_path, "  default: [read_only, payments]\n  billing: [read_only]\n"
    )
    valid = _by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["valid"]
    assert not valid.passed
    assert "permissive-default" in valid.detail


def test_modules_layout_fails_valid_on_a_dead_grant(tmp_path) -> None:
    # A module lint: the boundary denies what a capability grants.
    ws = _modules_workspace(
        tmp_path, "  default: [read_only]\n  billing: [read_only, payments]\n"
    )
    (ws / "policies" / "boundaries" / "no_views.yaml").write_text(
        "tools:\n  view_orders: { mode: deny }\n"
    )
    valid = _by_name(score({"agent": AGENT}, ws, snapshot(ws), ""))["valid"]
    assert not valid.passed
    assert "dead-grant" in valid.detail


def test_an_empty_policy_is_valid_and_denies(tmp_path) -> None:
    # As `hexgate policy validate` reads it: an empty file is an empty policy.
    ws = _workspace(tmp_path, "# nothing granted yet\n")
    case = {
        "agent": AGENT,
        "expect": {
            "decisions": [{"role": "default", "tool": "view_orders", "expect": "deny"}]
        },
    }
    checks = score(case, ws, snapshot(ws), "")
    assert all(c.passed for c in checks), checks
