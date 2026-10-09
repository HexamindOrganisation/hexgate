"""A case's dry-run calls are completed and checked as its agent would make them."""

from __future__ import annotations

import copy
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
from hexgate.egress.tcp import tcp_to_args
from hexgate.manifest.models import InputSchema
from tests.evals.helpers import AGENT, agent_view, manifest_tool

REFUND = manifest_tool("refund_order", order_id="string", amount="number")
KNOWN = Known(
    schemas={"refund_order": InputSchema.model_validate(REFUND["input_schema"])},
    skills={"triage"},
    agents={AGENT, "ops-bot"},
    attr_types={"tier": {"string"}},
)
REFUND_500 = {"tool": "refund_order", "args": {"order_id": "o1", "amount": 500}}
# A skill script call, completed by `policy.complete_call`.
SCRIPT_RUN = {
    "skill": "triage",
    "via": "script",
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
        {"refund_order": InputSchema.model_validate(REFUND["input_schema"])},
        set(),
        {AGENT, "draft-bot"},
    )


# ---------------------------------------------------------------- complete


@pytest.mark.parametrize(
    ("call", "args"),
    [
        (
            {"tool": "net.tcp_connect", "args": {"host": "db", "port": 5432}},
            tcp_to_args("db", 5432),
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
        # A manifest tool's, or an agent or skill gate's, are left as they are:
        # the scorer's `policy.complete_call` completes the gates.
        (REFUND_500, REFUND_500["args"]),
        ({"tool": "agent.tool:ops-bot"}, {}),
    ],
)
def test_complete_fills_the_arguments_the_proxies_send(call: dict, args: dict) -> None:
    call = copy.deepcopy(call)
    assert complete(call) == []
    assert call["args"] == args


@pytest.mark.parametrize(
    ("call", "error"),
    [
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
        (
            {
                "tool": "net.http_request",
                "args": {"method": "GET", "url": "http://x:99999/"},
            },
            "Port out of range",
        ),
        (
            {
                "tool": "net.http_request",
                "args": {"method": "GET", "url": "http://[x/"},
            },
            "Invalid IPv6 URL",
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
        # The proxy derives host, port and path from the URL.
        (
            {
                "tool": "net.http_request",
                "args": {"method": "GET", "url": "http://api.x.com/", "host": "evil"},
            },
            "host='evil', sent 'api.x.com'",
        ),
    ],
)
def test_complete_rejects_what_no_gate_sends(call: dict, error: str) -> None:
    assert re.search(error, str(complete(copy.deepcopy(call))))


# ---------------------------------------------------------------- unknown_names


@pytest.mark.parametrize(
    ("call", "unknown"),
    [
        (REFUND_500, []),
        ({**REFUND_500, "attributes": {"tier": "gold"}}, []),
        ({"tool": "agent.run", "args": {"agent": AGENT}}, []),
        ({"tool": "net.http_request", "args": {"host": "x"}}, []),
        ({"tool": "agent.tool:ops-bot"}, []),
        ({"tool": "skill:triage", "args": {"skill": "triage"}}, []),
        ({"tool": "skill.script:triage", "args": SCRIPT_RUN}, []),
        # Only a script carries its invocation arguments.
        (
            {"tool": "skill:triage", "args": {"skill": "triage", "script_args": None}},
            ["args.script_args"],
        ),
        # A misspelt name is denied whatever the policy says.
        ({"tool": "refund_ordr"}, ["refund_ordr"]),
        ({**REFUND_500, "args": {"amout": 1}}, ["args.amout"]),
        ({**REFUND_500, "attributes": {"region": "x"}}, ["ctx.region"]),
        ({"tool": "agent.toool:ops-bot"}, ["agent.toool:ops-bot"]),
        ({"tool": "agent.tool:ops-bott"}, ["agent.tool:ops-bott"]),
        ({"tool": "skill:triag", "args": {"skill": "triag"}}, ["skill:triag"]),
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


def test_an_unset_optional_argument_may_be_null() -> None:
    # Google records `Optional[int]` as "integer"; a model leaves it null.
    schema = {**REFUND["input_schema"]}
    schema["properties"] = {
        **schema["properties"],
        "limit": {"title": "limit", "type": "integer"},
    }
    known = Known(
        schemas={"refund_order": InputSchema.model_validate(schema)},
        skills=set(),
        agents={AGENT},
        attr_types={},
    )
    call = {**REFUND_500, "args": {"order_id": "o1", "amount": 1, "limit": None}}
    assert bad_values(call, known) == []


def test_load_known_counts_a_manifest_s_sub_agent_as_a_reach_target(
    tmp_path: Path,
) -> None:
    # Registered on its own only with `serve --register-subagents`.
    view = agent_view(AGENT, REFUND)
    view["manifest"]["subagents"] = [{"name": "refund-sub", "via": "tool"}]
    (tmp_path / "agents.json").write_text(json.dumps([view]))
    known = load_known(tmp_path, AGENT)
    assert unknown_names({"tool": "agent.tool:refund-sub"}, known) == []
