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
from evals.policy_writing.sources import SourceError
from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.egress.tcp import tcp_to_args
from hexgate.manifest.models import AgentManifest, InputSchema
from tests.evals.helpers import AGENT, agent_view, manifest_tool

REFUND = manifest_tool("refund_order", order_id="string", amount="number")
TRIAGE = {"name": "triage", "description": "triage"}
HASH = "sha256:ab12"


def _known(*tools: dict, **fields) -> Known:
    """`AGENT`'s Known, with `ops-bot` registered too; `fields` set its manifest's."""
    manifest = agent_view(AGENT, *tools, **fields)["manifest"]
    return Known(
        AGENT,
        AgentManifest.model_validate(manifest),
        frozenset({AGENT, "ops-bot"}),
        {"tier": {"string"}},
    )


KNOWN = _known(REFUND, skills=[TRIAGE])
REFUND_500 = {"tool": "refund_order", "args": {"order_id": "o1", "amount": 500}}
# A skill script call, completed by `policy.complete_call`.
SCRIPT_RUN = {
    "skill": "triage",
    "via": "script",
    "file_path": "scripts/run.sh",
    "content_hash": HASH,
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
    [known] = load_known(tmp_path, AGENT)
    assert (known.agent, known.schemas, known.registered, known.attr_types) == (
        AGENT,
        KNOWN.schemas,
        {AGENT, "ops-bot"},
        {"tier": {"string"}},
    )
    assert [s.name for s in known.manifest.skills] == ["triage"]


def test_load_known_reads_the_endpoint_s_loose_shape(tmp_path: Path) -> None:
    # The endpoint sends null for a missing description, skills or guards, and
    # for an agent registered with no manifest yet.
    view = agent_view(AGENT, {**REFUND, "description": None})
    draft = {**agent_view("draft-bot"), "manifest": None}
    (tmp_path / "agents.json").write_text(json.dumps([draft, view]))
    [known] = load_known(tmp_path, AGENT)
    assert (known.schemas, known.manifest.skills, known.registered) == (
        {"refund_order": InputSchema.model_validate(REFUND["input_schema"])},
        [],
        {AGENT, "draft-bot"},
    )


def test_when_the_case_has_no_agent_then_load_known_reads_every_agent(
    tmp_path: Path,
) -> None:
    # A role-wide case: each agent with a manifest, any agent's attributes.
    other = agent_view("ops-bot", manifest_tool("wire_transfer", iban="string"))
    draft = {**agent_view("draft-bot"), "manifest": None}
    views = [agent_view(AGENT, REFUND), other, draft]
    (tmp_path / "agents.json").write_text(json.dumps(views))
    rows = [{"agent_name": "ops-bot", "attributes": {"eu": True}}]
    (tmp_path / "audit.json").write_text(json.dumps(rows))
    known = load_known(tmp_path, None)
    assert [(k.agent, k.attr_types) for k in known] == [
        ("ops-bot", {"eu": {"boolean"}}),
        (AGENT, {"eu": {"boolean"}}),
    ]


def test_when_no_agent_has_a_manifest_then_load_known_raises(tmp_path: Path) -> None:
    draft = {**agent_view("draft-bot"), "manifest": None}
    (tmp_path / "agents.json").write_text(json.dumps([draft]))
    with pytest.raises(SourceError, match="no agent in agents.json has a manifest"):
        load_known(tmp_path, None)


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
        # LangChain hides agent-as-tool edges, so a tool reach to another
        # registered agent may be real; a handoff or a self-reach never is.
        ({"tool": "agent.handoff:ops-bot"}, ["agent.handoff:ops-bot"]),
        ({"tool": f"agent.tool:{AGENT}"}, [f"agent.tool:{AGENT}"]),
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
    known = _known({**REFUND, "input_schema": schema})
    call = {**REFUND_500, "args": {"order_id": "o1", "amount": 1, "limit": None}}
    assert bad_values(call, known) == []


@pytest.mark.parametrize(
    ("tool", "unknown"),
    [
        ("agent.tool:refund-sub", []),
        # The edge is a tool call: a handoff to it is denied whatever the policy.
        ("agent.handoff:refund-sub", ["agent.handoff:refund-sub"]),
        # A recorded edge list is complete, so a registered agent isn't one.
        ("agent.tool:ops-bot", ["agent.tool:ops-bot"]),
    ],
)
def test_a_reach_target_needs_the_manifest_s_edge_for_its_via(
    tool: str, unknown: list[str]
) -> None:
    # Registered on its own only with `serve --register-subagents`.
    known = _known(REFUND, subagents=[{"name": "refund-sub", "via": "tool"}])
    assert unknown_names({"tool": tool}, known) == unknown


# ---------------------------------------------------------------- skill args


@pytest.mark.parametrize(
    ("tool", "args", "framework", "bad"),
    [
        ("skill:triage", {"file_path": "/skills/triage/SKILL.md"}, "langchain", []),
        # Google's skill load passes no path.
        ("skill:triage", {"file_path": None}, "google", []),
        ("skill:triage", {"file_path": None}, "langchain", ["args.file_path missing"]),
        (
            "skill.resource:triage",
            {"file_path": None},
            "google",
            ["args.file_path missing"],
        ),
        ("skill.script:triage", SCRIPT_RUN, "google", []),
        # The adapters hash the skill's body; a null hash fails any pin on it.
        (
            "skill:triage",
            {"file_path": "/skills/triage/SKILL.md", "content_hash": None},
            "langchain",
            ["args.content_hash=None, sent sha256:<hex>"],
        ),
        (
            "skill.script:triage",
            {**SCRIPT_RUN, "content_hash": "ab12"},
            "google",
            ["args.content_hash='ab12', sent sha256:<hex>"],
        ),
        # Spelled otherwise than the adapter sends it, a path fails any
        # constraint on it: LangChain's is absolute, Google's script under scripts/.
        (
            "skill.resource:triage",
            {"file_path": "skills/triage/forms.md"},
            "langchain",
            ["args.file_path='skills/triage/forms.md', sent '/skills/triage/forms.md'"],
        ),
        (
            "skill.script:triage",
            {**SCRIPT_RUN, "file_path": "run.sh"},
            "google",
            ["args.file_path='run.sh', sent 'scripts/run.sh'"],
        ),
        # Google sends a resource's path as the model gives it.
        ("skill.resource:triage", {"file_path": "forms.md"}, "google", []),
    ],
)
def test_bad_values_on_a_skill_call(
    tool: str, args: dict, framework: str, bad: list[str]
) -> None:
    known = _known(skills=[TRIAGE], framework=framework)
    call = {"tool": tool, "args": {"content_hash": HASH, **args}}
    assert bad_values(call, known) == bad
