"""The names a policy may use (tools, skills, guards, arguments, caller
attributes), and the ones it invents.

Known names come from the starting project's stand-ins for the Hexgate MCP:
`agents.json` (manifests) and `audit.json` (audit rows, the only source of
caller-attribute names).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.security.constraints import iter_arg_refs, parse_constraint
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


def load_known_names(ws: Path, agent: str) -> KnownNames:
    """The names a policy for `agent` may use.

    The starting project stands in for what the Hexgate MCP's tools (per its
    design) return:
    - `agents.json` is `agents_list` (`GET /projects/{id}/agents/manifest`, a
      list of `AgentManifestView`). Tools with their argument names, skills and
      guards come from `agent`'s manifest only, so a tool another agent in the
      project has is unknown.
    - `audit.json` is `audit_decisions`: `AuditDecisionRow`s, as a list or the
      endpoint's page (`{rows, ...}`). Caller attributes are set per request and
      are not in the manifest, so the known ones are the `attributes` keys of
      `agent`'s rows. No `audit.json` means no known attributes.
    """
    views = json.loads((ws / AGENTS_JSON).read_text())
    view = next(
        (v for v in views if v["name"] == agent and v.get("manifest") is not None),
        None,
    )
    if view is None:
        raise ValueError(f"agents.json has no manifest for agent {agent!r}")
    # Read as the endpoint returns it, not as the SDK registers it: the platform's
    # AgentManifestView is looser (a tool's `description` may be null).
    manifest = view["manifest"]
    audit = ws / AUDIT_JSON
    rows = json.loads(audit.read_text()) if audit.exists() else []
    if isinstance(rows, dict):  # the endpoint's page shape, {rows, total, ...}
        rows = rows["rows"]
    attrs = {
        name
        for row in rows
        if row["agent_name"] == agent
        for name in row.get("attributes") or {}
    }
    return KnownNames(
        tools={
            t["name"]: set(t["input_schema"]["properties"]) for t in manifest["tools"]
        },
        attrs=attrs,
        skills={s["name"] for s in manifest.get("skills") or []},
        guards={g["name"] for g in manifest.get("guards") or []},
    )


def _roles(policy_set: PolicySet) -> list[AgentPolicy]:
    """Each role as it is enforced: inheritance and file-level constraints merged."""
    return [policy_set.policy_for(role) for role in policy_set.roles]


def unknown_keys(policy_set: PolicySet, known: KnownNames) -> list[str]:
    """Tools, skills (`skill:<name>`) and guards (`guard:<name>`) a policy keys on
    that the manifest doesn't list. Synthetic tool keys are always known: the
    `<target>` of `agent.<via>:<target>` is not checked against `agents.json`."""
    bad = set()
    for p in _roles(policy_set):
        bad |= {
            t
            for t in p.tools
            if t not in known.tools and t not in SYNTHETIC_ARGS and not is_agent_key(t)
        }
        # Trimmed as the runtime trims them, so ` pdf ` governs the skill `pdf`.
        skills = {canonical_skill_name(s) for s in p.skills}
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


def unknown_refs(policy_set: PolicySet, known: KnownNames) -> list[str]:
    """Constraint paths that read a name the manifest or audit rows don't define."""
    every_arg = set().union(
        *known.tools.values(),
        *SYNTHETIC_ARGS.values(),
        AGENT_REACH_ARGS,
        SKILL_SCRIPT_ARGS,
    )
    bad = set()
    for p in _roles(policy_set):
        for tool, text in _constraint_lines(p):
            allowed = _allowed_args(tool, known.tools, every_arg)
            bad |= {
                f"{tool or 'policy-level'}: {'.'.join(path)}"
                for path in iter_arg_refs(parse_constraint(text))
                if not _path_ok(path, allowed, known.attrs)
            }
    return sorted(bad)
