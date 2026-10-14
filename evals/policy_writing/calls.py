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

from evals.policy_writing.policy import manifest_can_call
from evals.policy_writing.sources import (
    SourceError,
    load_attribute_values,
    load_project_agents,
)
from hexgate.adapters.google.tools import _canonical_script_path
from hexgate.adapters.langchain.skills import canonical_skill_path
from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.egress.tcp import tcp_to_args
from hexgate.manifest.models import AgentFramework, AgentManifest, InputSchema
from hexgate.security.models import (
    AGENT_RUN_TOOL,
    gate_args,
    is_agent_reach_key,
    is_skill_key,
    skill_key,
)
from hexgate.security.network import (
    EGRESS_TOOL_ARGS,
    NET_HTTP_REQUEST,
    NET_TCP_CONNECT,
)
from hexgate.security.skill_gate import CONTENT_HASH_PREFIX

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

    agent: str
    manifest: AgentManifest
    registered: frozenset[str]  # agents.json's agents, the possible reach targets
    attr_types: dict[str, set[str]]  # each attribute, with the JSON types it arrived as

    @property
    def schemas(self) -> dict[str, InputSchema]:
        """The agent's tools."""
        return {t.name: t.input_schema for t in self.manifest.tools}


def load_known(project: Path, agent: str | None) -> list[Known]:
    """What `agent` knows, from `agents.json` and `audit.json`; with no `agent`
    (a role-wide case), what each agent with a manifest knows, any agent's
    attributes counting for each, as the scorer's name checks count them.

    Raises `SourceError` for a malformed file, an agent with no manifest, or
    no manifest at all.
    """
    agents = load_project_agents(project)
    if agent is not None and agent not in agents.manifests:
        raise SourceError(f"agents.json has no manifest for agent {agent!r}")
    if not agents.manifests:
        raise SourceError("no agent in agents.json has a manifest")
    values = load_attribute_values(project, agent)
    attr_types = {k: {_json_type(v) for v in vs} for k, vs in values.items()}
    return [
        Known(name, manifest, agents.registered, attr_types)
        for name, manifest in sorted(agents.manifests.items())
        if agent in (None, name)
    ]


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
    A tool, skill or reach target is known when the agent can make the call
    (`policy.manifest_can_call`, as the scorer's `can_call` judges a role-wide case): a reach key
    needs the sub-agent edge for that `via`, not just a registered target.
    """
    tool, args = call["tool"], call.get("args", {})
    if not manifest_can_call(known.manifest, known.agent, tool, known.registered):
        return [tool]
    if tool in EGRESS_TOOL_ARGS:
        allowed = EGRESS_TOOL_ARGS[tool]
    elif tool == AGENT_RUN_TOOL or is_agent_reach_key(tool) or is_skill_key(tool):
        allowed = gate_args(tool)
    else:
        allowed = set(known.schemas[tool].properties)
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


def _bad_skill_args(tool: str, args: dict, framework: AgentFramework) -> list[str]:
    """The skill gate's args no adapter sends: a null `content_hash` (it hashes
    the skill's body); a null `file_path` on a read or a script (the path
    read or run), or on a LangChain skill load (its `SKILL.md`); a `file_path`
    spelled otherwise than the adapter sends it. A constraint on either denies
    those, so a `deny` check would pass whatever the policy.
    """
    bad = []
    content_hash = args.get("content_hash")
    if not (
        isinstance(content_hash, str) and content_hash.startswith(CONTENT_HASH_PREFIX)
    ):
        bad.append(
            f"args.content_hash={content_hash!r}, sent {CONTENT_HASH_PREFIX}<hex>"
        )
    loads = tool.startswith(skill_key("instructions", ""))
    path = args.get("file_path")
    if path is None:
        if not loads or framework is AgentFramework.LANGCHAIN:
            bad.append("args.file_path missing")
    elif (sent := _sent_skill_path(tool, path, framework)) != path:
        # `skills/pdf/a.md` for `/skills/pdf/a.md` fails any path constraint.
        bad.append(f"args.file_path={path!r}, sent {sent!r}")
    return bad


def _sent_skill_path(tool: str, path, framework: AgentFramework):
    """`path` as the agent's adapter puts it in the skill decision."""
    if not isinstance(path, str):
        return path
    if framework is AgentFramework.LANGCHAIN:
        return canonical_skill_path(path)
    if framework is AgentFramework.GOOGLE and tool.startswith(skill_key("script", "")):
        return _canonical_script_path(path)
    return path


def bad_values(call: dict, known: Known) -> list[str]:
    """Values `call` gives its tool's arguments or the caller's attributes that
    no real call would carry. Run after `unknown_names` finds nothing."""
    tool, args = call["tool"], call.get("args", {})
    if is_skill_key(tool):
        bad = _bad_skill_args(tool, args, known.manifest.framework)
    else:
        schema = known.schemas.get(tool)
        bad = _bad_args(args, schema) if schema else []
    # A quoted "2" for an int attribute is denied by `ctx.clearance >= 2`
    # whatever the threshold.
    return bad + [
        f"ctx.{k}={v!r} is not {' or '.join(sorted(known.attr_types[k]))}"
        for k, v in call.get("attributes", {}).items()
        if _json_type(v) not in known.attr_types[k]
    ]
