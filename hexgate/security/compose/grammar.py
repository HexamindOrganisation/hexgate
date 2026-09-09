"""The ``policy.yaml`` authoring grammar (position-wildcard shape).

These pydantic models are the *authoring* surface — a single entry file plus
imports — distinct from the SDK's resolved :class:`~hexgate.security.models.AgentPolicy`.
:mod:`hexgate.security.compose.lower` turns a parsed :class:`Entry` into the
``ModuleContent`` lists the existing linker fold consumes; nothing here touches
the engine.

Shape (scope by depth — a block keyword is a sibling of the ``agents``/``roles``
name-map, never a key inside it):

    version, import, export          # top-level only
    boundary / tools / reach / mcp / admission   # at any scope → that scope
    agents: { <name>: agent-body }   # top level only; body may add roles:
    roles:  { <name>: role-body }    # agent-body only

A ``tools`` block at the top level means all agents + all roles; in an agent body,
that agent + all its roles; in a role body, that one cell.
"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from hexgate.security.models import AgentVia

# Keywords that are structural, so an agent or role may not be *named* one — a
# `roles: { tools: … }` would be unreadable even though the parser could tell it
# from the block. Enforced on every name-map.
RESERVED_NAMES = frozenset(
    {
        "version",
        "import",
        "export",
        "boundary",
        "tools",
        "reach",
        "mcp",
        "admission",
        "agents",
        "roles",
    }
)

GrantMode = Literal["allow", "approval_required"]
CeilingMode = Literal["allow", "approval_required", "deny"]


def _as_list(v: object) -> object:
    """Accept ``constraint: "x"`` as sugar for ``constraints: ["x"]``."""
    return [v] if isinstance(v, str) else v


class _ConstraintsMixin(BaseModel):
    """Shared ``constraint``/``constraints`` ergonomics for every spec."""

    # No populate_by_name: only the YAML aliases are accepted (e.g. `as:`, not the
    # `via` field name), so a field-name spelling is rejected by extra="forbid".
    model_config = ConfigDict(extra="forbid")

    constraints: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _fold_singular_constraint(cls, data: object) -> object:
        if isinstance(data, dict) and "constraint" in data:
            data = dict(data)
            single = data.pop("constraint")
            if "constraints" in data:
                raise ValueError(
                    "specify either 'constraint' (one) or 'constraints' (a list), "
                    "not both"
                )
            data["constraints"] = _as_list(single)
        return data


class GrantSpec(_ConstraintsMixin):
    """A ``tools``/``mcp`` grant. Grants only ever allow or gate on approval."""

    mode: GrantMode = "allow"


class ReachSpec(_ConstraintsMixin):
    """A ``reach`` edge to a named target agent, per transfer mode (``as``)."""

    mode: GrantMode = "allow"
    # ``as`` is a Python keyword; expose it as the YAML key via an alias.
    via: list[AgentVia] = Field(default_factory=lambda: ["tool", "handoff"], alias="as")

    @field_validator("via", mode="before")
    @classmethod
    def _one_or_many(cls, v: object) -> object:
        return [v] if isinstance(v, str) else v


class CeilingSpec(_ConstraintsMixin):
    """A ``boundary`` entry — a ceiling. Caps only: it permits up to a constraint
    (``allow``/``approval_required``) or subtracts (``deny``); it never grants."""

    mode: CeilingMode = "allow"


class BoundaryBlock(BaseModel):
    """A ceiling block. ``default_policy`` is fail-closed deny by construction, so
    a tool (or reach edge) it does not list is denied — the closed-world posture.

    ``reach`` here is the ceiling's *allow-list* of permitted reach edges (each an
    optional cap): agent keys are closed-world, so a reach not listed in the
    boundary is denied even if a capability grants it. To permit reaching a
    target, the boundary must list it.
    """

    model_config = ConfigDict(extra="forbid")

    tools: dict[str, CeilingSpec] = Field(default_factory=dict)
    reach: dict[str, ReachSpec] = Field(default_factory=dict)
    # The ceiling on ingress: may any role *start* this agent at all? Closed-world
    # like reach — with no admission ceiling, an authored admission grant is
    # intersected to deny, so a boundary must permit admission to allow it.
    admission: CeilingSpec | None = None


class _GrantScope(BaseModel):
    """The grant blocks legal at any scope (role body, agent body, top level),
    plus an ``import:`` list — position is scope, so a fragment imported here
    applies at exactly this depth."""

    # No populate_by_name: only the `import` alias is accepted, so the `imports`
    # field-name spelling is rejected by extra="forbid" (no silent second spelling).
    model_config = ConfigDict(extra="forbid")

    boundary: BoundaryBlock | None = None
    tools: dict[str, GrantSpec] = Field(default_factory=dict)
    reach: dict[str, ReachSpec] = Field(default_factory=dict)
    mcp: dict[str, GrantSpec] = Field(default_factory=dict)
    # Ingress grant: may this scope's role start/enter this agent? Lowers to the
    # ``agent.run`` key the runtime AgentGate enforces. A single grant (it is about
    # *this* agent), boundary-ceilinged like reach.
    admission: GrantSpec | None = None
    imports: list[str] = Field(default_factory=list, alias="import")

    # Resolved leaf fragments spliced at this scope — populated by the import
    # resolver, never authored. PrivateAttr so it is not YAML-settable, not
    # validated, and not serialized. Elements are leaf ``_GrantScope``s (a
    # ``RoleBlock`` export, or a whole-file ``Entry`` with no agents).
    _imported: list["_GrantScope"] = PrivateAttr(default_factory=list)
    # Source file this scope came from, for provenance on lowered ModuleContents.
    # Local scopes keep the entry file; the resolver stamps each imported fragment
    # with the file it was loaded from.
    _source: str = PrivateAttr(default="policy.yaml")

    @model_validator(mode="after")
    def _no_tools_mcp_collision(self) -> "_GrantScope":
        # mcp is sugar for tools (same key namespace), so a name in both blocks in
        # one scope would silently clobber — reject it at parse, source-named.
        clash = sorted(set(self.tools) & set(self.mcp))
        if clash:
            raise ValueError(
                f"tool name(s) {clash} declared in both 'tools' and 'mcp' in the "
                f"same scope; mcp is sugar for tools — declare each once"
            )
        return self


class RoleBlock(_GrantScope):
    """A ``(agent, role)`` leaf — grants + an optional per-role ceiling."""


class AgentBlock(_GrantScope):
    """One agent's body: its all-roles grants/ceiling + a ``roles`` name-map."""

    roles: dict[str, RoleBlock] = Field(default_factory=dict)

    @field_validator("roles")
    @classmethod
    def _no_reserved_role_names(
        cls, value: dict[str, RoleBlock]
    ) -> dict[str, RoleBlock]:
        _reject_reserved(value, "role")
        return value


class Entry(_GrantScope):
    """The entry ``policy.yaml`` — the import-graph root.

    Carries the top-level (all-agents, all-roles) grant blocks plus the ``agents``
    name-map, and the file-level ``version`` / ``import`` / ``export``.
    """

    version: int = 1
    exports: dict[str, RoleBlock] = Field(default_factory=dict, alias="export")
    agents: dict[str, AgentBlock] = Field(default_factory=dict)

    # Only the `export` alias is accepted (not the `exports` field name).
    model_config = ConfigDict(extra="forbid")

    @field_validator("agents")
    @classmethod
    def _no_reserved_agent_names(
        cls, value: dict[str, AgentBlock]
    ) -> dict[str, AgentBlock]:
        _reject_reserved(value, "agent")
        return value


def _reject_reserved(names: dict[str, object], kind: str) -> None:
    clash = sorted(set(names) & RESERVED_NAMES)
    if clash:
        raise ValueError(
            f"{kind} name(s) {clash} are reserved keywords; an {kind} may not be "
            f"named one of {sorted(RESERVED_NAMES)}"
        )
