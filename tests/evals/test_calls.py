"""A case's dry-run calls are completed and checked as its agent would make them."""

from __future__ import annotations

import copy
import datetime
import json
import re
from pathlib import Path

import pytest

from evals.policy_writing.calls import (
    Known,
    bad_values,
    complete,
    load_known,
    unknown_names,
)
from hexgate.egress.model import connect_to_args, http_to_args
from tests.evals.helpers import AGENT, agent_view, manifest_tool

REFUND = manifest_tool("refund_order", order_id="string", amount="number")
KNOWN = Known(
    tools={"refund_order": {"order_id", "amount"}},
    attrs={"tier"},
    schemas={"refund_order": REFUND["input_schema"]},
    skills={"triage"},
    agents={AGENT, "ops-bot"},
    attr_types={"tier": {"string"}},
)
REFUND_500 = {"tool": "refund_order", "args": {"order_id": "o1", "amount": 500}}
SCRIPT_RUN = {
    "file_path": "scripts/run.sh",
    "content_hash": None,
    "script_args": None,
    "short_options": None,
    "positional_args": None,
}


# ---------------------------------------------------------------- load_known


@pytest.mark.parametrize("page", [True, False])
def test_load_known_reads_the_agent_s_manifest_and_audit_rows(
    tmp_path: Path, page: bool
) -> None:
    view = agent_view(AGENT, REFUND)
    view["manifest"]["skills"] = [{"name": "triage", "description": "triage"}]
    other = agent_view("ops-bot", manifest_tool("wire_transfer", iban="string"))
    (tmp_path / "agents.json").write_text(json.dumps([view, other]))
    rows = [
        {"agent_name": AGENT, "attributes": {"tier": "gold"}},
        {"agent_name": "ops-bot", "attributes": {"eu": 1}},
    ]
    # A plain list, or the endpoint's page, as `audit_decisions` returns it.
    audit = {"rows": rows, "total": 2} if page else rows
    (tmp_path / "audit.json").write_text(json.dumps(audit))
    assert load_known(tmp_path, AGENT) == KNOWN


def test_load_known_reads_the_endpoint_s_loose_shape(tmp_path: Path) -> None:
    # The endpoint sends null for a missing description, skills or guards, and
    # for an agent registered with no manifest yet.
    view = agent_view(AGENT, {**REFUND, "description": None})
    draft = {**agent_view("draft-bot"), "manifest": None}
    (tmp_path / "agents.json").write_text(json.dumps([draft, view]))
    known = load_known(tmp_path, AGENT)
    assert (known.schemas, known.skills, known.agents) == (
        {"refund_order": REFUND["input_schema"]},
        set(),
        {AGENT, "draft-bot"},
    )


# ---------------------------------------------------------------- complete


@pytest.mark.parametrize(
    ("call", "args"),
    [
        # The agent gates send their own args, which `policy.decide` fills in.
        ({"tool": "agent.tool:ops-bot"}, {}),
        ({"tool": "agent.run"}, {}),
        (
            {"tool": "skill:triage", "args": {"file_path": "f", "content_hash": None}},
            {
                "skill": "triage",
                "via": "instructions",
                "file_path": "f",
                "content_hash": None,
            },
        ),
        (
            {"tool": "skill.script:triage", "args": {**SCRIPT_RUN}},
            {"skill": "triage", "via": "script", **SCRIPT_RUN},
        ),
        (
            {"tool": "net.tcp_connect", "args": {"host": "db", "port": 5432}},
            {"host": "db", "port": 5432, "protocol": "tcp"},
        ),
        (
            {
                "tool": "net.http_request",
                "args": {"method": "GET", "url": "http://x.com/a"},
            },
            http_to_args("GET", "http://x.com/a"),
        ),
        (
            {
                "tool": "net.http_request",
                "args": {"method": "CONNECT", "host": "x.com", "port": 443},
            },
            connect_to_args("x.com", 443),
        ),
        # Spelt out in full, as the proxy builds it.
        (
            {"tool": "net.http_request", "args": connect_to_args("x.com", 443)},
            connect_to_args("x.com", 443),
        ),
        # A manifest tool's arguments are the case's own.
        (REFUND_500, REFUND_500["args"]),
    ],
)
def test_complete_fills_the_arguments_the_gates_send(call: dict, args: dict) -> None:
    call = copy.deepcopy(call)
    assert complete(call, AGENT) == []
    assert call["args"] == args


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (
            {"tool": "agent.tool:ops-bot", "args": {"via": "tool"}},
            r"the gate sends its own args; drop \['via'\]",
        ),
        ({"tool": "agent.run", "args": {"agent": AGENT}}, "drop \\['agent'\\]"),
        ({"tool": "skill:triage", "args": {"skill": "billing"}}, "skill='billing'"),
        ({"tool": "skill:triage"}, "args.file_path missing"),
        ({"tool": "net.tcp_connect", "args": {"host": "db", "port": "443"}}, "'443'"),
        (
            {
                "tool": "net.tcp_connect",
                "args": {"host": "db", "port": 1, "protocol": "udp"},
            },
            "protocol='udp', sent 'tcp'",
        ),
        # The gate always sends these, so a constraint on one denies without it.
        (
            {"tool": "net.http_request", "args": {"host": "x.com"}},
            "args.method missing",
        ),
        # HTTPS reaches the gate only as a CONNECT tunnel, which carries no path.
        (
            {
                "tool": "net.http_request",
                "args": {"method": "GET", "url": "https://x/"},
            },
            "an https url is a CONNECT",
        ),
        (
            {
                "tool": "net.http_request",
                "args": {
                    "method": "CONNECT",
                    "host": "x.com",
                    "port": 443,
                    "path": "/",
                },
            },
            "args.path is never sent",
        ),
        # A client behind the proxy sends an absolute http:// URI, upper case.
        (
            {"tool": "net.http_request", "args": {"method": "GET", "url": "x.com/a"}},
            "not an absolute http:// url",
        ),
        (
            {"tool": "net.http_request", "args": {"method": "get", "url": "http://x/"}},
            "not upper case",
        ),
        # A script's invocation arguments are null when left out, never absent.
        (
            {
                "tool": "skill.script:triage",
                "args": {"file_path": "f", "content_hash": None},
            },
            "args.script_args missing",
        ),
        # The proxy derives host, port and path from the URL.
        (
            {
                "tool": "net.http_request",
                "args": {"method": "GET", "url": "http://api.x.com/", "host": "evil"},
            },
            "host='evil', sent 'api.x.com'",
        ),
        # An unquoted YAML date is no value a real call carries.
        (
            {"tool": "refund_order", "args": {"order_id": datetime.date(2026, 3, 1)}},
            "order_id=datetime.date.* is not JSON",
        ),
    ],
)
def test_complete_rejects_what_no_gate_sends(call: dict, error: str) -> None:
    assert re.search(error, str(complete(copy.deepcopy(call), AGENT)))


# ---------------------------------------------------------------- unknown_names


@pytest.mark.parametrize(
    ("call", "unknown"),
    [
        (REFUND_500, []),
        ({**REFUND_500, "attributes": {"tier": "gold"}}, []),
        ({"tool": "net.http_request", "args": {"host": "x"}}, []),
        ({"tool": "agent.tool:ops-bot"}, []),
        ({"tool": "skill:triage", "args": {"skill": "triage"}}, []),
        ({"tool": "skill.script:triage", "args": {**SCRIPT_RUN}}, []),
        # Only a script carries its invocation arguments.
        ({"tool": "skill:triage", "args": {"script_args": None}}, ["args.script_args"]),
        # A misspelt name is denied whatever the policy says.
        ({"tool": "refund_ordr"}, ["refund_ordr"]),
        ({**REFUND_500, "args": {"amout": 1}}, ["args.amout"]),
        ({**REFUND_500, "attributes": {"region": "x"}}, ["ctx.region"]),
        ({"tool": "agent.toool:ops-bot"}, ["agent.toool:ops-bot"]),
        ({"tool": "agent.tool:ops-bott"}, ["agent.tool:ops-bott"]),
        ({"tool": "skill:triag"}, ["skill:triag"]),
        # Another agent's tool is not this agent's.
        ({"tool": "wire_transfer"}, ["wire_transfer"]),
    ],
)
def test_unknown_names(call: dict, unknown: list[str]) -> None:
    assert unknown_names(call, KNOWN) == unknown


# ---------------------------------------------------------------- bad_values


@pytest.mark.parametrize(
    ("call", "bad"),
    [
        (REFUND_500, []),
        # Left out, or of another type: denied once a constraint reads it.
        ({**REFUND_500, "args": {"order_id": "o1"}}, ["args.amount missing"]),
        (
            {**REFUND_500, "args": {"order_id": "o1", "amount": "51"}},
            ["args.amount='51' is not number"],
        ),
        # A blank `amount:` is null.
        (
            {**REFUND_500, "args": {"order_id": "o1", "amount": None}},
            ["args.amount=None is not number"],
        ),
        (
            {**REFUND_500, "args": {"order_id": "o1", "amount": True}},
            ["args.amount=True is not number"],
        ),
        # The audit rows carry `tier` as a string.
        ({**REFUND_500, "attributes": {"tier": 2}}, ["ctx.tier=2 is not string"]),
    ],
)
def test_bad_values(call: dict, bad: list[str]) -> None:
    assert bad_values(call, KNOWN) == bad


@pytest.mark.parametrize("value", [2, ["a"], None, True])
def test_a_string_schema_says_nothing_about_the_value(value) -> None:
    # Adapters record "string" for `int | None`, `bool | None` and lists, and
    # OpenAI's strict schemas list optional arguments as required, sent as null.
    call = {**REFUND_500, "args": {"order_id": value, "amount": 1}}
    assert bad_values(call, KNOWN) == []
