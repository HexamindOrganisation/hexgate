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
    And,
    Call,
    Cmp,
    ConstraintParseError,
    Count,
    Not,
    Or,
    Quant,
    Ref,
    parse_constraint,
)
from hexgate.security.models import AGENT_RUN_TOOL, agent_target_key
from hexgate.security.network import NET_HTTP_REQUEST, NET_TCP_CONNECT

# Arguments the synthetic keys carry, copied from the gates that build those calls
# (hexgate/egress/model.py and tcp.py, hexgate/security/agent_gate.py);
# tests/evals/test_names.py fails if a gate's built arguments drift from these.
AGENT_REACH_ARGS = frozenset({"agent", "target", "via"})
SYNTHETIC_ARGS = {
    NET_HTTP_REQUEST: frozenset(
        {"method", "scheme", "host", "port", "url", "path", "query"}
    ),
    NET_TCP_CONNECT: frozenset({"host", "port", "protocol"}),
    AGENT_RUN_TOOL: frozenset({"agent"}),
}
# What a constraint path may start with: anything else parses, then never matches.
ROOTS = {"args", "ctx", "run", "role", "tool"}


def _paths(node) -> Iterator[tuple[str, ...]]:
    """Every context path (`args.amount`, `ctx.department`) a parsed constraint reads."""
    if isinstance(node, Ref):
        yield node.path
    elif isinstance(node, Count):
        yield node.ref.path
    elif isinstance(node, Cmp):
        yield from _paths(node.left)
        yield from _paths(node.right)
    elif isinstance(node, Call):
        yield from _paths(node.arg)
    elif isinstance(node, Quant):
        yield from _paths(node.ref)
        yield from _paths(node.body)
    elif isinstance(node, And | Or):
        for part in node.parts:
            yield from _paths(part)
    elif isinstance(node, Not):
        yield from _paths(node.inner)


def load_known_names(ws: Path, agent: str) -> tuple[dict[str, set[str]], set[str]]:
    """({tool: its argument names}, caller attribute names) a policy for `agent` may use.

    The starting project stands in for what the Hexgate MCP's tools (per its
    design) return:
    - `agents.json` is `agents_list` (`GET /agents/manifest`, a list of
      `AgentManifestView`). Tools and their argument names come from `agent`'s
      manifest only, so a tool another agent in the project has is unknown.
    - `audit.json` is `audit_decisions`: `AuditDecisionRow`s, as a list or the
      endpoint's page (`{rows, ...}`). Caller attributes are set per request and
      are not in the manifest, so the known ones are the `attributes` keys of
      `agent`'s rows. No `audit.json` means no known attributes.
    """
    manifests = {
        view["name"]: AgentManifest.model_validate(view["manifest"])
        for view in json.loads((ws / "agents.json").read_text())
        if view.get("manifest") is not None
    }
    if agent not in manifests:
        raise ValueError(f"agents.json has no manifest for agent {agent!r}")
    tools = {t.name: set(t.input_schema.properties) for t in manifests[agent].tools}
    audit = ws / "audit.json"
    rows = json.loads(audit.read_text()) if audit.exists() else []
    if isinstance(rows, dict):  # the endpoint's page shape, {rows, total, ...}
        rows = rows["rows"]
    attrs = {
        name
        for row in rows
        if row["agent_name"] == agent
        for name in row.get("attributes") or {}
    }
    return tools, attrs


def policy_bodies(doc: dict) -> tuple[dict, list[dict]]:
    bodies = [b for b in (doc.get("roles") or {}).values() if isinstance(b, dict)]
    return doc, bodies or [doc]


def policy_tools(doc: dict) -> set[str]:
    _, bodies = policy_bodies(doc)
    return {t for b in bodies for t in (b.get("tools") or {})}


def unknown_refs(doc: dict, tools: dict[str, set[str]], attrs: set[str]) -> list[str]:
    """`args.x` / `ctx.x` a constraint uses that `tools` / `attrs` don't define."""
    every_arg = set().union(*tools.values(), *SYNTHETIC_ARGS.values(), AGENT_REACH_ARGS)
    doc, bodies = policy_bodies(doc)
    # (tool or None for a policy- or role-level constraint, constraint text)
    lines = [(None, c) for c in doc.get("constraints") or []]
    for b in bodies:
        lines += [(None, c) for c in b.get("constraints") or []]
        lines += [
            (None, c) for c in (b.get("default_policy") or {}).get("constraints") or []
        ]
        lines += [
            (AGENT_RUN_TOOL, c)
            for c in (b.get("admission") or {}).get("constraints") or []
        ]
        # A skill constraint can read any call's args, like a policy-level one.
        for spec in (b.get("skills") or {}).values():
            lines += [(None, c) for c in (spec or {}).get("constraints") or []]
        for target, spec in (b.get("agents") or {}).items():
            key = agent_target_key("tool", target)  # both vias carry the same args
            lines += [(key, c) for c in (spec or {}).get("constraints") or []]
        for tool, spec in (b.get("tools") or {}).items():
            lines += [(tool, c) for c in (spec or {}).get("constraints") or []]
    bad = set()
    for tool, text in lines:
        try:
            paths = list(_paths(parse_constraint(str(text))))
        except ConstraintParseError:
            continue  # `valid` already fails on it
        for path in paths:
            kind, name = path[0], ".".join(path[1:2])
            if kind not in ROOTS:
                ok = False
            elif kind == "run":
                ok = name in KNOWN_RUN_PATHS
            elif kind in ("role", "tool"):
                ok = not name  # a plain string: `role.x` never matches
            elif not name:
                continue
            elif kind == "ctx":
                ok = name in attrs
            elif tool is None:
                ok = name in every_arg
            elif tool in SYNTHETIC_ARGS:
                ok = name in SYNTHETIC_ARGS[tool]
            elif tool.startswith("agent."):  # a reach key, agent.<via>:<target>
                ok = name in AGENT_REACH_ARGS
            else:
                ok = (
                    tool not in tools or name in tools[tool]
                )  # unknown tools fail elsewhere
            if not ok:
                bad.add(f"{tool or 'policy-level'}: {'.'.join(path)}")
    return sorted(bad)
