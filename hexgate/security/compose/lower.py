"""Lower a parsed :class:`Entry` into the ``ModuleContent`` lists the linker fold
consumes — one ``(boundaries, capabilities)`` pair per resolved role.

This is the whole point of the front-end: the new grammar is *assembly above the
fold*. Each grant block becomes a capability ``ModuleContent``; each ``boundary``
becomes a boundary ``ModuleContent``; the linker unions the grants and intersects
the ceilings exactly as it does for the tier-folder layout, so the engine, rego,
and signing paths never change.

Scope by depth (per the grammar): the top-level blocks apply to every agent and
role; an agent body's blocks to that agent's roles; a role body's blocks to that
one cell. Roles live under a named agent, so the generic/unnamed agent (``"*"``)
resolves a single ``default`` role from the top-level blocks alone.
"""

from __future__ import annotations

from hexgate.security.compose.grammar import (
    AgentBlock,
    BoundaryBlock,
    Entry,
    _GrantScope,
)
from hexgate.security.module_loader import _canonical_hash
from hexgate.security.models import AgentPolicy, AgentTargetPolicy, BaseToolPolicy
from hexgate.security.modules import DEFAULT_AGENT, ModuleContent
from hexgate.security.policy_set import DEFAULT_ROLE_NAME

# The single role every project has: the generic agent, and any named agent with
# no `roles:` map, resolve to it. Reuse the SDK's constant so the two can't drift.
DEFAULT_ROLE = DEFAULT_ROLE_NAME


def _content_hash(name: str, policy: AgentPolicy) -> str:
    """Stable identity for a synthetic fragment — reuses the loader's canonical
    hash so compose and file-loaded modules serialize identically (and non-JSON
    YAML scalars are handled via its ``default=str``, not re-raised here)."""
    return _canonical_hash({"name": name, "policy": policy.model_dump(mode="json")})


def _grants_policy(scope: _GrantScope) -> AgentPolicy | None:
    """An :class:`AgentPolicy` carrying a scope's ``tools``/``mcp``/``reach`` grants
    (never a boundary). ``None`` when the scope grants nothing."""
    # mcp is readability sugar — an MCP tool is a tool; it composes identically
    # (same key namespace). A name in both blocks is rejected at parse (the
    # grammar's _no_tools_mcp_collision), so here they simply merge.
    tools: dict[str, BaseToolPolicy] = {}
    for name, spec in scope.tools.items():
        tools[name] = BaseToolPolicy(mode=spec.mode, constraints=spec.constraints)
    for name, spec in scope.mcp.items():
        tools[name] = BaseToolPolicy(mode=spec.mode, constraints=spec.constraints)
    agents: dict[str, AgentTargetPolicy] = {}
    for target, spec in scope.reach.items():
        agents[target] = AgentTargetPolicy(
            via=spec.via, mode=spec.mode, constraints=spec.constraints
        )
    if not tools and not agents:
        return None
    return AgentPolicy(tools=tools, agents=agents)


def _boundary_policy(block: BoundaryBlock | None) -> AgentPolicy | None:
    """An :class:`AgentPolicy` ceiling (``default_policy: deny``) for a boundary
    block. ``None`` when there is no boundary at this scope."""
    if block is None:
        return None
    tools = {
        name: BaseToolPolicy(mode=spec.mode, constraints=spec.constraints)
        for name, spec in block.tools.items()
    }
    agents = {
        target: AgentTargetPolicy(
            via=spec.via, mode=spec.mode, constraints=spec.constraints
        )
        for target, spec in block.reach.items()
    }
    return AgentPolicy(
        default_policy=BaseToolPolicy(mode="deny"), tools=tools, agents=agents
    )


def _cap(name: str, policy: AgentPolicy, source: str) -> ModuleContent:
    return ModuleContent(
        name=name,
        kind="capability",
        policy=policy,
        source=f"{source}#{name}",
        content_hash=_content_hash(name, policy),
    )


def _boundary(name: str, policy: AgentPolicy, source: str) -> ModuleContent:
    return ModuleContent(
        name=name,
        kind="boundary",
        policy=policy,
        source=f"{source}#{name}",
        content_hash=_content_hash(name, policy),
    )


def _cell(
    scopes: list[tuple[str, _GrantScope]], agent: str, role: str
) -> tuple[list[ModuleContent], list[ModuleContent]]:
    """Build the ``(boundaries, capabilities)`` ModuleContent lists for one cell.

    Each scope contributes its own blocks *and* its imported leaf fragments
    (``scope._imported``); grants union and boundaries intersect in the fold, so
    an imported fragment is just an extra contribution — never a merge/overwrite.
    """
    caps: list[ModuleContent] = []
    boundaries: list[ModuleContent] = []
    for label, scope in scopes:
        for idx, frag in enumerate((scope, *scope._imported)):
            tag = (
                f"{label}@{agent}/{role}"
                if idx == 0
                else f"{label}#imp{idx}@{agent}/{role}"
            )
            grants = _grants_policy(frag)
            if grants is not None:
                caps.append(_cap(tag, grants, frag._source))
            ceiling = _boundary_policy(frag.boundary)
            if ceiling is not None:
                boundaries.append(_boundary(f"boundary:{tag}", ceiling, frag._source))
    return boundaries, caps


def lower(entry: Entry, agent: str = DEFAULT_AGENT) -> dict[str, tuple[list, list]]:
    """Lower ``entry`` for one executing ``agent`` → ``{role: (boundaries, caps)}``.

    Every result carries a ``default`` role (resolved from the base scopes alone),
    matching the SDK's always-present default; a named agent also resolves each
    role declared under it, folding the top-level, agent-level, and role-level
    scopes together. The generic/unnamed agent (``"*"``) has only the ``default``
    role from the top-level blocks (roles live under a named agent).

    Imports must already be resolved onto each scope's ``_imported`` (see
    :func:`hexgate.security.compose.imports.resolve_imports`).
    """
    agent_block: AgentBlock | None = (
        entry.agents.get(agent) if agent != DEFAULT_AGENT else None
    )

    # Scopes that apply regardless of role: top-level always; the agent body when
    # a named agent is being resolved.
    base_scopes: list[tuple[str, _GrantScope]] = [("top", entry)]
    if agent_block is not None:
        base_scopes.append((f"agent:{agent}", agent_block))

    # The default role: base scopes only (no role-specific block).
    out: dict[str, tuple[list, list]] = {
        DEFAULT_ROLE: _cell(base_scopes, agent, DEFAULT_ROLE)
    }
    # Each named role: base scopes + that role's block.
    for role, role_block in (agent_block.roles if agent_block else {}).items():
        out[role] = _cell([*base_scopes, (f"role:{role}", role_block)], agent, role)
    return out
