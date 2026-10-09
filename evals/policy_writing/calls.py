"""Check and complete the dry-run calls a case lists.

The scorer dry-runs each call in a case's `decisions` and `superset` probes.
The SDK denies a call that names a tool, skill or agent it doesn't know, leaves
out an argument a constraint reads, or gives one a value of the wrong type, so
any such slip in a case would pass every `deny` check whatever the policy says.

- `complete` fills in what an egress proxy derives from a `net.*` call and
  returns what the call contradicts or lacks. Agent and skill gates are
  completed by the scorer's own `policy.complete_call`.
- `unknown_names` returns the names a call uses that its agent doesn't know.
- `bad_values` returns the values its agent would never send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from evals.policy_writing.sources import (
    SourceError,
    load_attribute_values,
    load_project_agents,
)
from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.egress.tcp import tcp_to_args
from hexgate.manifest.models import InputSchema
from hexgate.security.models import (
    AGENT_RUN_TOOL,
    agent_reach_target,
    gate_args,
    is_agent_reach_key,
    is_skill_key,
)
from hexgate.security.network import (
    EGRESS_TOOL_ARGS,
    NET_HTTP_REQUEST,
    NET_TCP_CONNECT,
)

# JSON-schema types an argument value must have.
_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


@dataclass(frozen=True)
class Known:
    """What one agent's calls may use, from its starting project."""

    schemas: dict[str, InputSchema]  # the agent's tools
    skills: set[str]
    agents: set[str]  # the possible reach targets: agents.json's, and sub-agents
    attr_types: dict[str, set[str]]  # each attribute, with the JSON types it arrived as


def load_known(project: Path, agent: str) -> Known:
    """`agent`'s known names and types, from `agents.json` and `audit.json`.

    Raises `SourceError` for a malformed file or an agent with no manifest.
    """
    agents = load_project_agents(project)
    if agent not in agents.manifests:
        raise SourceError(f"agents.json has no manifest for agent {agent!r}")
    manifest = agents.manifests[agent]
    values = load_attribute_values(project, agent)
    return Known(
        schemas={t.name: t.input_schema for t in manifest.tools},
        skills={k.name for k in manifest.skills or []},
        # A manifest's sub-agent is a reach target too, as the SDK counts it.
        agents=set(agents.registered)
        | {s.name for m in agents.manifests.values() for s in m.subagents or []},
        attr_types={k: {_json_type(v) for v in vs} for k, vs in values.items()},
    )


def _is_a(value, want) -> bool:
    # bool is an int in Python, not a number in JSON.
    return isinstance(value, want) and (want is bool) == isinstance(value, bool)


def _json_type(value) -> str:
    for name, kind in _TYPES.items():
        if name != "integer" and _is_a(value, kind):
            return name
    return "null"


# ---------------------------------------------------------------- complete


@dataclass
class _Sent:
    """What an egress proxy sends for one call, given what the call spells out."""

    args: dict = field(default_factory=dict)  # what the proxy derives
    required: tuple[str, ...] = ()  # what the call must give, not null
    problems: list[str] = field(default_factory=list)


def _tcp(args: dict) -> _Sent:
    sent = _Sent(required=("host", "port"))
    if isinstance(args.get("host"), str) and _is_a(args.get("port"), int):
        sent.args = tcp_to_args(args["host"], args["port"])
    return sent


def _http(args: dict) -> _Sent:
    """What the proxy builds from the request line (hexgate/egress/model.py)."""
    if args.get("method") == "CONNECT":
        sent = _Sent(required=("host", "port"))
        if isinstance(args.get("host"), str) and _is_a(args.get("port"), int):
            sent.args = connect_to_args(args["host"], args["port"])
        return sent
    sent = _Sent(required=("method", "url"))
    method, url = args.get("method"), args.get("url")
    if not (isinstance(method, str) and isinstance(url, str)):
        return sent
    try:
        parts = urlsplit(url)
    except ValueError as exc:  # a malformed IPv6 host
        sent.problems.append(f"url={url!r}: {exc}")
        return sent
    if parts.scheme.lower() == "https":
        # The proxy sees HTTPS only as a CONNECT tunnel: no path, no GET.
        sent.problems.append("an https url is a CONNECT with host and port")
    elif parts.scheme.lower() != "http" or not parts.hostname:
        # A client behind the proxy always sends an absolute http:// URI.
        sent.problems.append(f"url={url!r} is not an absolute http:// url")
    elif method != method.upper():
        sent.problems.append(f"method={method!r} is not upper case")
    else:
        try:
            sent.args = http_to_args(method, url)
        except ValueError as exc:  # a port out of range or not a number
            sent.problems.append(f"url={url!r}: {exc}")
    return sent


_PROXIES = {NET_TCP_CONNECT: _tcp, NET_HTTP_REQUEST: _http}


def complete(call: dict) -> list[str]:
    """Fill in what `call`'s egress proxy derives; what it contradicts or lacks.

    A dry-run without those args, or with other values, is denied by any
    constraint on them, so it would pass every `deny` check.
    """
    args = call.setdefault("args", {})
    proxy = _PROXIES.get(call["tool"])
    if proxy is None:
        return []
    sent = proxy(args)
    bad = list(sent.problems)
    bad += [f"args.{k} missing" for k in sent.required if args.get(k) is None]
    if args.get("port") is not None and not _is_a(args["port"], int):
        bad.append(f"args.port={args['port']!r} is not an int")
    bad += [
        f"args.{k}={args[k]!r}, sent {v!r}"
        for k, v in sent.args.items()
        if args.get(k, v) != v
    ]
    if sent.args:  # a proxy sends nothing else
        bad += [f"args.{k} is never sent" for k in sorted(set(args) - set(sent.args))]
    args.update(sent.args)
    return bad


# ---------------------------------------------------------------- check


def unknown_names(call: dict, known: Known) -> list[str]:
    """Tool, skill, agent, argument and attribute names `call` uses that are unknown.

    The call is completed first: a gate call carries its gate's key and args.
    """
    tool, args = call["tool"], call.get("args", {})
    if tool in EGRESS_TOOL_ARGS:
        allowed = EGRESS_TOOL_ARGS[tool]
    elif tool == AGENT_RUN_TOOL:
        allowed = gate_args(tool)
    elif is_agent_reach_key(tool):
        if agent_reach_target(tool) not in known.agents:
            return [tool]
        allowed = gate_args(tool)
    elif is_skill_key(tool):
        if args["skill"] not in known.skills:
            return [tool]
        allowed = gate_args(tool)
    elif tool in known.schemas:
        allowed = set(known.schemas[tool].properties)
    else:
        return [tool]
    bad = [f"args.{a}" for a in args if a not in allowed]
    return bad + [
        f"ctx.{a}" for a in call.get("attributes", {}) if a not in known.attr_types
    ]


def _bad_args(args: dict, schema: InputSchema) -> list[str]:
    """Required arguments left out, and values of another type than the schema's.

    YAML reads a quoted `"51"` as a string and a blank `amount:` as null.
    """
    bad = [f"args.{a} missing" for a in schema.required if a not in args]
    for name, value in args.items():
        kind = schema.properties[name].type
        # Adapters record "string" for any schema without one top-level type
        # (`int | None`, `bool | None`, a list, a model), so it says nothing.
        # Google records `Optional[int]` as "integer", so null passes on an
        # argument that isn't required; on a required precise one it is a
        # blank value.
        if value is None and name not in schema.required:
            continue
        want = None if kind == "string" else _TYPES.get(kind)
        if want and not _is_a(value, want):
            bad.append(f"args.{name}={value!r} is not {kind}")
    return bad


def bad_values(call: dict, known: Known) -> list[str]:
    """Values `call` gives its tool's arguments or the caller's attributes that
    no real call would carry. Run after `unknown_names` finds nothing."""
    schema = known.schemas.get(call["tool"])
    bad = _bad_args(call.get("args", {}), schema) if schema else []
    # A quoted "2" for an int attribute is denied by `ctx.clearance >= 2`
    # whatever the threshold.
    return bad + [
        f"ctx.{k}={v!r} is not {' or '.join(sorted(known.attr_types[k]))}"
        for k, v in call.get("attributes", {}).items()
        if _json_type(v) not in known.attr_types[k]
    ]
