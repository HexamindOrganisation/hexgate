"""The names a policy may use (tools, skills, guards, arguments, caller
attributes), and the ones it invents.

Known names come from the starting project's stand-ins for the Hexgate MCP:
`agents.json` (manifests) and `audit.json` (audit rows, the only source of
caller-attribute names).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.security import load_local_modules, load_roles
from hexgate.security.constraints import iter_arg_refs, parse_constraint
from hexgate.security.linker import resolve_role_map
from hexgate.security.models import (
    AGENT_RUN_TOOL,
    AgentPolicy,
    is_agent_key,
    is_agent_reach_key,
    is_skill_key,
    skill_key,
)
from hexgate.security.naming import canonical_skill_name
from hexgate.security.network import NET_HTTP_REQUEST, NET_TCP_CONNECT
from hexgate.security.policy_set import PolicySet

# Arguments the synthetic keys carry. `net.http_request`'s come from the egress
# proxy's own builders; the rest are copied from the gates that build those calls
# (hexgate/egress/tcp.py, hexgate/security/agent_gate.py, and the skill seams in
# hexgate/adapters/langchain/skills.py and google/tools.py), and
# tests/evals/test_names.py fails if a gate's built arguments drift from these.
# The adapters' agent-as-tool seams build their reach arguments inline, mirroring
# agent_gate.py's ReachGate; only ReachGate is tested.
AGENT_REACH_ARGS = frozenset({"agent", "target", "via"})
# A skill call at any level (`skill:`, `skill.resource:`); a script
# (`skill.script:`) also carries its invocation arguments.
SKILL_ARGS = frozenset({"skill", "via", "file_path", "content_hash"})
SKILL_SCRIPT_ARGS = SKILL_ARGS | {"script_args", "short_options", "positional_args"}
SYNTHETIC_ARGS = {
    NET_HTTP_REQUEST: frozenset(
        connect_to_args("h", 443).keys() | http_to_args("GET", "http://h/").keys()
    ),
    NET_TCP_CONNECT: frozenset({"host", "port", "protocol"}),
    AGENT_RUN_TOOL: frozenset({"agent"}),
}
# What a constraint path may start with: anything else parses, then never matches.
# Loading already rejects an unknown `run.*` path.
ROOTS = {"args", "ctx", "run", "role", "tool"}


# The files the names come from, the starting project's stand-ins for the MCP.
AGENTS_JSON, AUDIT_JSON = NAME_SOURCES = ("agents.json", "audit.json")


@dataclass(frozen=True)
class KnownNames:
    tools: dict[str, set[str]]  # tool name: its argument names
    attrs: set[str]  # caller attributes (`ctx.*`)
    skills: set[str]
    guards: set[str]
    agents: set[str]  # what a reach key (`agent.<via>:<target>`) may target


def load_known_names(ws: Path, agent: str | None) -> KnownNames:
    """The names a policy for `agent` may use (any agent's when None).

    The starting project stands in for what the Hexgate MCP's tools (per its
    design) return:
    - `agents.json` is `agents_list` (`GET /projects/{id}/agents/manifest`, a
      list of `AgentManifestView`): tools with their argument names, skills and
      guards. With `agent` (a case editing one agent's policy), only its manifest
      counts, so a tool another agent in the project has is unknown. Without it
      (a role or project-wide edit), every agent's does. Reach targets are the
      project's agents and any sub-agent a manifest names, whoever the case is for.
    - `audit.json` is `audit_decisions`: `AuditDecisionRow`s, as a list or the
      endpoint's page (`{rows, ...}`). Caller attributes are set per request and
      are not in the manifest, so the known ones are the `attributes` keys of
      `agent`'s rows (every row's when None). No `audit.json` means none.
    """
    views = json.loads((ws / AGENTS_JSON).read_text())
    agents = {v["name"] for v in views}
    named = _named(views)
    manifests = [m for name, m in named if agent in (None, name)]
    if agent is not None and not manifests:
        raise ValueError(f"agents.json has no manifest for agent {agent!r}")
    tools: dict[str, set[str]] = {}
    for m in manifests:
        for t in m["tools"]:
            tools.setdefault(t["name"], set()).update(t["input_schema"]["properties"])
    return KnownNames(
        tools=tools,
        attrs=_attributes(ws, agent),
        # The endpoint sends `null` for an agent with none.
        skills={s["name"] for m in manifests for s in m.get("skills") or []},
        guards={g["name"] for m in manifests for g in m.get("guards") or []},
        agents=agents | {s["name"] for _, m in named for s in m.get("subagents") or []},
    )


def _named(views: list[dict]) -> list[tuple[str, dict]]:
    """(agent name, manifest) for each view that has a manifest. Read as the
    endpoint returns it rather than as the SDK registers it: AgentManifestView is
    looser (a tool's `description` may be null)."""
    return [(v["name"], v["manifest"]) for v in views if v.get("manifest") is not None]


def _attributes(ws: Path, agent: str | None) -> set[str]:
    """The `attributes` keys of `agent`'s audit.json rows (every row's when None)."""
    audit = ws / AUDIT_JSON
    rows = json.loads(audit.read_text()) if audit.exists() else []
    if isinstance(rows, dict):  # the endpoint's page shape, {rows, total, ...}
        rows = rows["rows"]
    return {
        name
        for row in rows
        if agent in (None, row["agent_name"])
        for name in row.get("attributes") or {}
    }


def enforced_roles(policy_set: PolicySet) -> list[AgentPolicy]:
    """Each role as it is enforced: inheritance and file-level constraints merged."""
    return [policy_set.policy_for(role) for role in policy_set.roles]


def unknown_keys(policies: Iterable[AgentPolicy], known: KnownNames) -> list[str]:
    """Tools, skills (`skill:<name>`) and guards (`guard:<name>`) a policy keys on
    that the manifest doesn't list, and reach keys (`agent.<via>:<target>`) whose
    target is no known agent. Other synthetic keys are always known."""
    bad = set()
    for p in policies:
        bad |= {
            t
            for t in p.tools
            if t not in known.tools
            and t not in SYNTHETIC_ARGS
            and not is_agent_key(t)
            and not is_skill_key(t)
        }
        bad |= {
            t
            for t in p.effective_tools  # where `agents:` lowers to reach keys
            if is_agent_reach_key(t) and t.partition(":")[2] not in known.agents
        }
        # Trimmed as the runtime trims them, so ` pdf ` governs the skill `pdf`.
        # A module tree's skills arrive lowered into `tools` (`skill.script:pdf`).
        skills = {canonical_skill_name(s) for s in p.skills}
        skills |= {t.split(":", 1)[1] for t in p.tools if is_skill_key(t)}
        bad |= {skill_key("instructions", s) for s in skills - known.skills}
        bad |= {f"guard:{g}" for g in p.guards.keys() - known.guards}
    return sorted(bad)


def _constraint_lines(p: AgentPolicy) -> Iterator[tuple[str | None, str]]:
    """(tool key the constraint applies to, or None for a policy-level one that
    may meet any call; its text)."""
    for c in [*p.constraints, *p.default_policy.constraints]:
        yield None, c
    for key, tool in p.effective_tools.items():
        yield from ((key, c) for c in tool.constraints)


def _allowed_args(
    tool: str | None, tools: dict[str, set[str]], every_arg: set[str]
) -> set[str] | None:
    """The `args.*` names a call under `tool` carries; None for an unknown tool."""
    if tool is None:
        return every_arg
    if tool in SYNTHETIC_ARGS:
        return SYNTHETIC_ARGS[tool]
    if is_agent_reach_key(tool):  # agent.<via>:<target>
        return AGENT_REACH_ARGS
    if tool.startswith(skill_key("script", "")):
        return SKILL_SCRIPT_ARGS
    if is_skill_key(tool):
        return SKILL_ARGS
    return tools.get(tool)


def _path_ok(path: tuple[str, ...], allowed: set[str] | None, attrs: set[str]) -> bool:
    kind, name = path[0], path[1] if len(path) > 1 else ""
    if kind not in ROOTS:
        return False
    if kind in ("role", "tool"):
        return not name  # a plain string: `role.x` never matches
    if kind == "run" or not name:
        return True
    if kind == "ctx":
        return name in attrs
    return allowed is None or name in allowed  # an unknown tool fails elsewhere


def unknown_refs(
    policies: Iterable[AgentPolicy], known: KnownNames, others: KnownNames | None = None
) -> list[str]:
    """Constraint paths that read a name the manifest or audit rows don't define.

    With `others`, a constraint under a tool only `others` has (another agent's)
    is checked against `others`: only that agent's calls reach it.
    """
    every_arg = set().union(
        *known.tools.values(),
        *SYNTHETIC_ARGS.values(),
        AGENT_REACH_ARGS,
        SKILL_SCRIPT_ARGS,
    )
    bad = set()
    for p in policies:
        for tool, text in _constraint_lines(p):
            names = known
            if others and tool in others.tools and tool not in known.tools:
                names = others
            allowed = _allowed_args(tool, names.tools, every_arg)
            bad |= {
                f"{tool or 'policy-level'}: {'.'.join(path)}"
                for path in iter_arg_refs(parse_constraint(text))
                if not _path_ok(path, allowed, names.attrs)
            }
    return sorted(bad)


def module_invented_names(
    ws: Path, agent: str | None, known: KnownNames
) -> tuple[list[str], list[str]]:
    """(`unknown_keys`, `unknown_refs`) of a module tree, file by file, each
    prefixed with its path. `known` is `agent`'s names.

    A resolved column holds only what that column imports, so each file is
    checked on its own: a capability in `agent`'s column against `agent`'s
    names, any other capability against every agent's. A boundary is org-wide:
    it may name any agent's tool, but a constraint on `agent`'s own tool (or on
    any call) is in `agent`'s bundle, so it may read only `agent`'s attributes.
    Raises if a file can't be read.
    """
    every = known if agent is None else load_known_names(ws, None)
    boundaries, capabilities = load_local_modules(ws)
    own = set()
    if agent is not None:
        columns = resolve_role_map(load_roles(ws), capabilities, agent)
        own = {m.name for caps in columns.values() for m in caps}
    # (file, names for its keys, names for its refs); a ref under another
    # agent's tool is checked against every agent's names (`others`).
    files = [(m, every, known) for m in boundaries] + [
        (m, names, names)
        for m in capabilities
        for names in [known if m.name in own else every]
    ]
    bad_keys, bad_refs = [], []
    for m, key_names, ref_names in files:
        note = "" if key_names is known else " (no agent has it)"
        rel = Path(m.source).relative_to(ws)
        bad_keys += [f"{rel}: {k}{note}" for k in unknown_keys([m.policy], key_names)]
        refs = unknown_refs([m.policy], ref_names, others=every)
        bad_refs += [f"{rel}: {r}" for r in refs]
    return bad_keys, bad_refs
