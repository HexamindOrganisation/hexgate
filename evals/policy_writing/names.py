"""The tool, argument and attribute names a policy may use, and the ones it invents.

Known names come from the starting project's stand-ins for the Hexgate MCP:
`agents.json` (manifests) and `audit.json` (audit rows, the only source of
caller-attribute names).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from hexgate.manifest.models import AgentManifest
from hexgate.runtime.run_facts import KNOWN_RUN_PATHS
from hexgate.security.constraints import (
    ConstraintParseError,
    iter_arg_refs,
    parse_constraint,
)
from hexgate.security.linker import resolve_role_map
from hexgate.security.models import (
    AGENT_RUN_TOOL,
    agent_target_key,
    is_agent_key,
    is_agent_reach_key,
    is_skill_key,
    skill_key,
)
from hexgate.security.network import NET_HTTP_REQUEST, NET_TCP_CONNECT

# Arguments the synthetic keys carry, copied from the gates that build those calls
# (hexgate/egress/model.py and tcp.py, hexgate/security/agent_gate.py, and the
# skill seams in hexgate/adapters/langchain/skills.py and google/tools.py);
# tests/evals/test_names.py fails if a gate's built arguments drift from these.
# The adapters' agent-as-tool seams build their reach arguments inline, mirroring
# agent_gate.py's ReachGate; only ReachGate is tested.
AGENT_REACH_ARGS = frozenset({"agent", "target", "via"})
# One `skills:` entry governs all three disclosure levels; only a script adds
# its invocation arguments.
SKILL_ARGS = frozenset(
    {"skill", "via", "file_path", "content_hash"}
    | {"script_args", "short_options", "positional_args"}
)
SYNTHETIC_ARGS = {
    NET_HTTP_REQUEST: frozenset(
        {"method", "scheme", "host", "port", "url", "path", "query"}
    ),
    NET_TCP_CONNECT: frozenset({"host", "port", "protocol"}),
    AGENT_RUN_TOOL: frozenset({"agent"}),
}
# What a constraint path may start with: anything else parses, then never matches.
ROOTS = {"args", "ctx", "run", "role", "tool"}


def load_known_names(
    ws: Path, agent: str | None
) -> tuple[dict[str, set[str]], set[str]]:
    """({tool: its argument names}, caller attribute names) the policy may use.

    The starting project stands in for what the Hexgate MCP's tools (per its
    design) return:
    - `agents.json` is `agents_list` (`GET /agents/manifest`, a list of
      `AgentManifestView`): tools and their argument names.
    - `audit.json` is `audit_decisions`: `AuditDecisionRow`s, as a list or the
      endpoint's page (`{rows, ...}`). Caller attributes are set per request and
      are not in the manifest, so the known ones are the rows' `attributes` keys.
      No `audit.json` means no known attributes.

    With `agent` (a case editing one agent's policy), only that agent's manifest
    and audit rows count, so a tool another agent has is unknown. Without it (a
    role or project-wide edit), every agent in the project counts.
    """
    manifests = {
        view["name"]: AgentManifest.model_validate(view["manifest"])
        for view in json.loads((ws / "agents.json").read_text())
        if view.get("manifest") is not None
    }
    if agent is not None and agent not in manifests:
        raise ValueError(f"agents.json has no manifest for agent {agent!r}")
    tools: dict[str, set[str]] = {}
    for name, manifest in manifests.items():
        if agent in (None, name):
            for t in manifest.tools:
                tools.setdefault(t.name, set()).update(t.input_schema.properties)
    audit = ws / "audit.json"
    rows = json.loads(audit.read_text()) if audit.exists() else []
    if isinstance(rows, dict):  # the endpoint's page shape, {rows, total, ...}
        rows = rows["rows"]
    attrs = {
        name
        for row in rows
        if agent in (None, row["agent_name"])
        for name in row.get("attributes") or {}
    }
    return tools, attrs


def _bodies(doc: dict) -> list[dict]:
    """Each role's body, or the whole file when it has no roles."""
    bodies = [b for b in (doc.get("roles") or {}).values() if isinstance(b, dict)]
    return bodies or [doc]


def unknown_tools(doc: dict, tools: dict[str, set[str]]) -> list[str]:
    """Tools a policy names that are neither in `tools` nor a synthetic key."""
    named = {t for b in _bodies(doc) for t in (b.get("tools") or {})}
    return sorted(
        t
        for t in named
        if t not in tools and t not in SYNTHETIC_ARGS and not is_agent_key(t)
    )


def _constraints(spec) -> list:
    return (spec or {}).get("constraints") or []


def _constraint_lines(doc: dict) -> Iterator[tuple[str | None, str]]:
    """(tool key the constraint applies to, or None for any call; its text)."""
    bodies = _bodies(doc)
    if bodies != [doc]:  # a role file's own constraints apply to every role
        yield from ((None, c) for c in _constraints(doc))
    keyed = (
        ("skills", lambda name: skill_key("instructions", str(name))),
        ("agents", lambda target: agent_target_key("tool", target)),  # both vias
        ("tools", lambda tool: tool),
    )
    for b in bodies:
        for spec in (b, b.get("default_policy")):
            yield from ((None, c) for c in _constraints(spec))
        yield from ((AGENT_RUN_TOOL, c) for c in _constraints(b.get("admission")))
        for section, key in keyed:
            for name, spec in (b.get(section) or {}).items():
                yield from ((key(name), c) for c in _constraints(spec))


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
    if is_skill_key(tool):
        return SKILL_ARGS
    return tools.get(tool)


def _path_ok(path: tuple[str, ...], allowed: set[str] | None, attrs: set[str]) -> bool:
    kind, name = path[0], path[1] if len(path) > 1 else ""
    if kind not in ROOTS:
        return False
    if kind == "run":
        return len(path) == 2 and name in KNOWN_RUN_PATHS
    if kind in ("role", "tool"):
        return not name  # a plain string: `role.x` never matches
    if not name:
        return True
    if kind == "ctx":
        return name in attrs
    return allowed is None or name in allowed  # an unknown tool fails elsewhere


def unknown_refs(doc: dict, tools: dict[str, set[str]], attrs: set[str]) -> list[str]:
    """Constraint paths that read a name `tools` / `attrs` don't define."""
    every_arg = set().union(
        *tools.values(), *SYNTHETIC_ARGS.values(), AGENT_REACH_ARGS, SKILL_ARGS
    )
    bad = set()
    for tool, text in _constraint_lines(doc):
        try:
            paths = iter_arg_refs(parse_constraint(str(text)))
        except ConstraintParseError:
            continue  # `valid` already fails on it
        allowed = _allowed_args(tool, tools, every_arg)
        bad |= {
            f"{tool or 'policy-level'}: {'.'.join(path)}"
            for path in paths
            if not _path_ok(path, allowed, attrs)
        }
    return sorted(bad)


def org_wide_tools(boundaries: list, capabilities: list, roles, agent: str) -> set[str]:
    """Boundary tools a policy for `agent` may name though `agent` lacks them.

    A boundary is org-wide, so it may deny another agent's tool (e.g. deny
    wire_transfer). Unless a capability in `agent`'s roles.yaml column grants
    it too: then `agent`'s policy grants a tool `agent` doesn't have.
    """
    columns = resolve_role_map(roles, capabilities, agent)
    granted = {t for caps in columns.values() for m in caps for t in m.policy.tools}
    return {t for m in boundaries for t in m.policy.tools} - granted


def module_unknowns(
    ws: Path, modules: list, tools: dict[str, set[str]], attrs: set[str]
) -> tuple[list[str], list[str]]:
    """(unknown tools, unknown refs) in each module file, prefixed with its path.

    A resolved column holds only what that column imports, so a file it leaves
    out (a capability for another agent, or for none) is checked on its own.
    """
    bad_tools, bad_refs = [], []
    for m in modules:
        doc = m.policy.model_dump()
        rel = Path(m.source).relative_to(ws)
        bad_tools += [
            f"{rel}: {t} (no agent has it)" for t in unknown_tools(doc, tools)
        ]
        bad_refs += [f"{rel}: {r}" for r in unknown_refs(doc, tools, attrs)]
    return bad_tools, bad_refs
