"""Check and complete the dry-run calls a case lists.

The scorer dry-runs each call in a case's `decisions` and `superset` probes.
The SDK denies a call that names a tool, skill or agent it doesn't know, leaves
out an argument a constraint reads, or gives one a value of the wrong type, so
any such slip in a case would pass every `deny` check whatever the policy says.

- `complete` fills in the arguments the skill and `net.*` gates always send,
  refuses args on an agent-gate call (the scorer fills those), and returns
  what a call contradicts or lacks.
- `unknown_names` returns the names a call uses that its agent doesn't know.
- `bad_values` returns the values its agent would never send.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import get_args
from urllib.parse import urlsplit

from evals.policy_writing.names import (
    AGENT_REACH_ARGS,
    SKILL_ARGS,
    SKILL_SCRIPT_ARGS,
    SYNTHETIC_ARGS,
    load_known_names,
)
from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.security.models import (
    AGENT_RUN_TOOL,
    SkillVia,
    is_agent_reach_key,
    is_skill_key,
    skill_key,
)
from hexgate.security.network import NET_HTTP_REQUEST, NET_TCP_CONNECT

# JSON-schema types an argument value must have.
_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}
_SKILL_VIA_BY_PREFIX = {skill_key(via, ""): via for via in get_args(SkillVia)}


@dataclass(frozen=True)
class Known:
    """What one agent's calls may use, from its starting project."""

    tools: dict[str, set[str]]  # {tool: argument names}, as the scorer reads them
    attrs: set[str]
    schemas: dict[str, dict]  # {tool: its input_schema, as agents.json has it}
    skills: set[str]
    agents: set[str]  # every agent in agents.json: the possible reach targets
    attr_types: dict[str, set[str]]  # the JSON types each attribute arrived with


def load_known(project: Path, agent: str) -> Known:
    """`agent`'s known names and types, from `agents.json` and `audit.json`.

    Raises OSError, ValueError, KeyError, TypeError or AttributeError for a
    missing or malformed file, or an agent with no manifest.
    """
    names = load_known_names(project, agent)  # the scorer's own reading
    views = json.loads((project / "agents.json").read_text())
    # The view load_known_names read; the endpoint's shape, so null fields are fine.
    manifest = next(
        v["manifest"] for v in views if v["name"] == agent and v.get("manifest")
    )
    audit = project / "audit.json"
    rows = json.loads(audit.read_text()) if audit.exists() else []
    attr_types: dict[str, set[str]] = {}
    for row in rows["rows"] if isinstance(rows, dict) else rows:
        if row["agent_name"] == agent:
            for name, value in (row.get("attributes") or {}).items():
                attr_types.setdefault(name, set()).add(_json_type(value))
    return Known(
        tools=names.tools,
        attrs=names.attrs,
        schemas={t["name"]: t["input_schema"] for t in manifest["tools"]},
        skills=names.skills,
        agents={v["name"] for v in views},
        attr_types=attr_types,
    )


def _is_a(value, want) -> bool:
    # bool is an int in Python, not a number in JSON.
    return isinstance(value, want) and (want is bool) == isinstance(value, bool)


def _json_type(value) -> str:
    for name, kind in _TYPES.items():
        if name != "integer" and _is_a(value, kind):
            return name
    return "null"


def _is_json(value) -> bool:
    # What a real call's arguments can hold; YAML also makes dates and times.
    if isinstance(value, list):
        return all(_is_json(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json(v) for k, v in value.items())
    return value is None or isinstance(value, (str, int, float, bool))


def _skill_parts(tool: str) -> tuple[str, str]:
    """(via, skill name) of a `skill:` / `skill.resource:` / `skill.script:` key."""
    prefix, name = tool.split(":", 1)
    return _SKILL_VIA_BY_PREFIX[f"{prefix}:"], name


def _reach_parts(tool: str) -> tuple[str, str]:
    """(via, target) of an `agent.<via>:<target>` key."""
    via, target = tool.removeprefix("agent.").split(":", 1)
    return via, target


# ---------------------------------------------------------------- complete


@dataclass
class _Sent:
    """What a gate sends for one call, and what the call must spell out itself."""

    args: dict = field(default_factory=dict)  # values the gate always sends
    required: tuple[str, ...] = ()  # present and not null
    present: tuple[str, ...] = ()  # present, null allowed
    exact: bool = False  # the gate sends nothing besides `args`
    problems: list[str] = field(default_factory=list)


def _agent_gate(tool: str, args: dict, agent: str) -> _Sent:
    # Admission and reach send their own args (agent, target, via), and
    # `policy.decide` fills them in itself and refuses a call that spells any.
    if not args:
        return _Sent()
    return _Sent(problems=[f"the gate sends its own args; drop {sorted(args)}"])


def _skill(tool: str, args: dict, agent: str) -> _Sent:
    # hexgate/adapters/langchain/skills.py and google/tools.py; content_hash may
    # be null, and a script's invocation arguments are null when the model
    # leaves them out, which a constraint reads otherwise than an absent one.
    via, name = _skill_parts(tool)
    present = ("file_path", "content_hash")
    if via == "script":
        present += ("script_args", "short_options", "positional_args")
    return _Sent({"skill": name, "via": via}, present=present)


def _tcp(tool: str, args: dict, agent: str) -> _Sent:
    return _Sent({"protocol": "tcp"}, required=("host", "port"))  # egress/tcp.py


def _http(tool: str, args: dict, agent: str) -> _Sent:
    """What the proxy builds from the request line (hexgate/egress/model.py)."""
    if args.get("method") == "CONNECT":
        sent = _Sent(required=("host", "port"), exact=True)
        if isinstance(args.get("host"), str) and _is_a(args.get("port"), int):
            sent.args = connect_to_args(args["host"], args["port"])
        return sent
    sent = _Sent(required=("method", "url"), exact=True)
    method, url = args.get("method"), args.get("url")
    if not (isinstance(method, str) and isinstance(url, str)):
        return sent
    parts = urlsplit(url)
    if parts.scheme.lower() == "https":
        # The proxy sees HTTPS only as a CONNECT tunnel: no path, no GET.
        sent.problems.append("an https url is a CONNECT with host and port")
    elif parts.scheme.lower() != "http" or not parts.hostname:
        # A client behind the proxy always sends an absolute http:// URI.
        sent.problems.append(f"url={url!r} is not an absolute http:// url")
    elif method != method.upper():
        sent.problems.append(f"method={method!r} is not upper case")
    else:
        sent.args = http_to_args(method, url)
    return sent


_GATES: dict[str, Callable[[str, dict, str], _Sent]] = {
    AGENT_RUN_TOOL: _agent_gate,
    NET_TCP_CONNECT: _tcp,
    NET_HTTP_REQUEST: _http,
}


def _gate(tool: str) -> Callable[[str, dict, str], _Sent] | None:
    if is_agent_reach_key(tool):
        return _agent_gate
    if is_skill_key(tool):
        return _skill
    return _GATES.get(tool)


def complete(call: dict, agent: str) -> list[str]:
    """Fill in the arguments `call`'s gate always sends; what it contradicts or lacks.

    A dry-run without them, or with other values, is denied by any constraint
    on them, so it would pass every `deny` check.
    """
    args = call.setdefault("args", {})
    bad = [f"args.{k}={v!r} is not JSON" for k, v in args.items() if not _is_json(v)]
    gate = _gate(call["tool"])
    if gate is None:
        return bad
    sent = gate(call["tool"], args, agent)
    bad += sent.problems
    bad += [f"args.{k} missing" for k in sent.required if args.get(k) is None]
    bad += [f"args.{k} missing" for k in sent.present if k not in args]
    if args.get("port") is not None and not _is_a(args["port"], int):
        bad.append(f"args.port={args['port']!r} is not an int")
    bad += [
        f"args.{k}={args[k]!r}, sent {v!r}"
        for k, v in sent.args.items()
        if args.get(k, v) != v
    ]
    if sent.exact and sent.args:
        bad += [f"args.{k} is never sent" for k in sorted(set(args) - set(sent.args))]
    args.update(sent.args)
    return bad


# ---------------------------------------------------------------- check


def unknown_names(call: dict, known: Known) -> list[str]:
    """Tool, skill, agent, argument and attribute names `call` uses that are unknown."""
    tool = call["tool"]
    if tool in SYNTHETIC_ARGS:
        args = SYNTHETIC_ARGS[tool]
    elif is_agent_reach_key(tool):
        if _reach_parts(tool)[1] not in known.agents:
            return [tool]
        args = AGENT_REACH_ARGS
    elif is_skill_key(tool):
        via, name = _skill_parts(tool)
        if name not in known.skills:
            return [tool]
        args = SKILL_SCRIPT_ARGS if via == "script" else SKILL_ARGS
    elif tool in known.tools:
        args = known.tools[tool]
    else:
        return [tool]
    bad = [f"args.{a}" for a in call.get("args", {}) if a not in args]
    return bad + [
        f"ctx.{a}" for a in call.get("attributes", {}) if a not in known.attrs
    ]


def _bad_args(args: dict, schema: dict) -> list[str]:
    """Required arguments left out, and values of another type than the schema's.

    YAML reads a quoted `"51"` as a string and a blank `amount:` as null.
    """
    bad = [f"args.{a} missing" for a in schema.get("required") or [] if a not in args]
    for name, value in args.items():
        kind = schema["properties"][name].get("type")
        # Adapters record "string" for any schema without one top-level type
        # (`int | None`, `bool | None`, a list, a model), so it says nothing; a
        # precise type rules out null too, since a nullable one is never precise.
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
