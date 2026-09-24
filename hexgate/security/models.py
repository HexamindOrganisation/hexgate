"""Pydantic models for agent security policies."""

from __future__ import annotations

from functools import cached_property
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from hexgate.security.constraints import parse_constraint
from hexgate.security.naming import canonical_name

PolicyMode = Literal["allow", "deny", "approval_required"]


def _parse_all(constraints: list[str]) -> list[str]:
    """Parse every constraint at load — a malformed expression is a config
    error, surfaced at ``model_validate`` time rather than at the first
    matching tool call."""
    for constraint in constraints:
        parse_constraint(constraint)
    return constraints


class BaseToolPolicy(BaseModel):
    """Define the access mode and per-call constraints for a single tool.

    ``constraints`` is a list of expression strings evaluated against the
    tool's invocation arguments (e.g. ``"args.amount <= 50"``). Every
    constraint must pass for the call to authorize. The grammar is parsed
    by :mod:`hexgate.security.constraints` — see that module for the full
    operator set. When the policy engine swaps to OPA/Rego in a later
    milestone, these strings carry through verbatim.
    """

    # extra="forbid" for the same reason AgentPolicy sets it: a mistyped field
    # (``contraints:``) would otherwise be dropped in silence, leaving
    # ``mode: allow`` with no fence at all. Inherited by FileToolPolicy and
    # AgentTargetPolicy, so it covers every nested policy scope.
    model_config = ConfigDict(extra="forbid")

    mode: PolicyMode = "deny"
    constraints: list[str] = Field(default_factory=list)

    @field_validator("constraints")
    @classmethod
    def _validate_constraint_grammar(cls, value: list[str]) -> list[str]:
        return _parse_all(value)


class FileScope(BaseModel):
    """Restrict a file-oriented tool to explicit path patterns."""

    model_config = ConfigDict(extra="forbid")

    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)


class FileToolPolicy(BaseToolPolicy):
    """Define access policy for file-oriented tools."""

    file_scope: FileScope | None = None


ToolPolicy = BaseToolPolicy | FileToolPolicy

AgentVia = Literal["tool", "handoff"]

# Reserved synthetic tool keys that agent-level gating lowers to. Kept here so
# the lowering and the agent gate (which builds the same keys at the seam) share
# one definition. ``.`` and ``:`` are safe in a tool name: both engines treat the
# name as an opaque string (the Rego compiler emits ``input.tool == "<name>"``),
# exactly as the ``net.*`` egress tools already do.
AGENT_RUN_TOOL = "agent.run"

# Prefixes for the reach keys (``agent.tool:<name>`` / ``agent.handoff:<name>``),
# derived from AgentVia so a new via mode is covered everywhere automatically. One
# source of truth for the namespace reservation and for both engines' closed-world
# handling, so pydantic and Rego cannot drift on which names are agent keys.
AGENT_REACH_PREFIXES = tuple(f"agent.{via}:" for via in get_args(AgentVia))


def agent_target_key(via: AgentVia, target: str) -> str:
    """Synthetic tool key for reaching ``target`` in a given transfer mode."""
    return f"agent.{via}:{target}"


def is_agent_via_key(name: str, via: AgentVia) -> bool:
    """True for a reach key of a specific ``via`` mode (``agent.<via>:``).

    Lets a caller tell agent-as-tool reach from handoff reach, e.g. to warn only
    about the mode a given adapter cannot enforce."""
    return name.startswith(f"agent.{via}:")


def is_agent_reach_key(name: str) -> bool:
    """True for an ``agent.tool:`` / ``agent.handoff:`` reach key.

    Distinguishes reach keys from admission (``agent.run``) for callers that gate
    the two differently at the seam. Both are closed-world at the engine
    (R-AGENT-002): an unlisted agent key denies regardless of ``default_policy``.
    Opt-in survives only as the gate's ``declares_admission()`` / ``declares_reach()``
    engagement check, not as an admit-on-absence fallback."""
    return name.startswith(AGENT_REACH_PREFIXES)


def is_agent_key(name: str) -> bool:
    """True for any synthetic agent-level key (``agent.run`` or a reach key).

    Used to reserve the ``agent.*`` namespace from authored tools. Both admission
    and reach are closed-world at the engine (R-AGENT-002); :func:`is_agent_reach_key`
    only separates the two for callers that need to tell a handoff/tool reach from
    admission (e.g. per-adapter warnings), not because they enforce differently."""
    return name == AGENT_RUN_TOOL or is_agent_reach_key(name)


SkillVia = Literal["instructions", "resource", "script"]

# Key prefix per disclosure level. ``skill:`` is the bare activation key (the
# instructions a skill discloses when it engages); the two deeper levels take a
# dotted qualifier, spelled like the agent reach keys. Keep ``skill:`` last: it is
# not a prefix of the other two (``skill.`` != ``skill:``), so the tuple order is
# irrelevant to ``str.startswith`` today, but a first-match loop would want the
# qualified spellings tried first.
_SKILL_PREFIX_BY_VIA: dict[SkillVia, str] = {
    "resource": "skill.resource:",
    "script": "skill.script:",
    "instructions": "skill:",
}
SKILL_PREFIXES = tuple(_SKILL_PREFIX_BY_VIA.values())


def skill_key(via: SkillVia, name: str) -> str:
    """Synthetic tool key for reaching one skill at one disclosure level.

    The skill name is canonicalized (:func:`~hexgate.security.naming.canonical_name`)
    exactly as :meth:`AgentPolicy.lowered_agent_tools` canonicalizes a reach target:
    the adapter builds this same key from the runtime skill's name, so a padded
    authored name must normalize to the key the seam looks up or the rule is inert.
    """
    return f"{_SKILL_PREFIX_BY_VIA[via]}{canonical_name(name)}"


def is_skill_key(name: str) -> bool:
    """True for a ``skill:`` / ``skill.resource:`` / ``skill.script:`` key.

    Reserves the namespace from authored tools, and (from M4) tells the engines
    which keys evaluate closed-world. A tool literally named ``skills`` is not one:
    the prefixes carry their separator."""
    return name.startswith(SKILL_PREFIXES)


class AgentTargetPolicy(BaseToolPolicy):
    """Authorize reaching one named target agent, per transfer mode.

    ``via`` names the transfer modes this rule governs: ``tool`` (agent-as-tool,
    the orchestrator keeps control) and/or ``handoff`` (control transfers). A
    target listed for ``tool`` only cannot be handed off to, and the reverse.
    ``mode`` and ``constraints`` behave exactly as on a tool policy.
    """

    via: list[AgentVia] = Field(default_factory=lambda: ["tool", "handoff"])

    @field_validator("via")
    @classmethod
    def _validate_via(cls, value: list[AgentVia]) -> list[AgentVia]:
        if not value:
            raise ValueError("via must list at least one of 'tool', 'handoff'")
        # De-dup, order-preserving.
        return list(dict.fromkeys(value))


class SkillPolicy(BaseToolPolicy):
    """Authorize one named skill, per disclosure level.

    ``via`` names the disclosure levels this rule governs: ``instructions`` (the
    skill's prose is put in front of the model), ``resource`` (its bundled files
    are readable) and ``script`` (its executables may run). A skill allowed for
    ``instructions`` only can be read but its scripts cannot be run.
    """

    via: list[SkillVia] = Field(
        default_factory=lambda: ["instructions", "resource", "script"]
    )

    @field_validator("via")
    @classmethod
    def _validate_via(cls, value: list[SkillVia]) -> list[SkillVia]:
        if not value:
            raise ValueError(
                "via must list at least one of 'instructions', 'resource', 'script'"
            )
        # De-dup, order-preserving.
        return list(dict.fromkeys(value))


class AgentPolicy(BaseModel):
    """Define an agent-wide tool authorization policy.

    ``inherits`` names other policy bundles whose ``tools`` map is merged
    in before this one's, left-to-right (later wins). Used for mixin
    policies like ``read_only`` that several roles share.

    ``is_mixin = True`` marks the policy as a building block — the SDK
    won't pick it as the effective policy for any HexgateContext scope; it can only
    be referenced via ``inherits``.

    ``constraints`` apply to every tool this role can reach, not just those
    falling through to ``default_policy`` — the place for a run-wide circuit
    breaker. Alone among these fields they **union** across ``inherits``:
    a child dropping a parent's fence would be fail-open.

    ``consts`` names reusable values referenced from constraints as
    ``consts.<name>`` (e.g. ``args.amount <= consts.max_refund``). Merged
    through ``inherits`` like ``tools`` — put shared constants in a mixin.

    Agent-level gating (both optional):

    * ``admission`` — ingress. May this role start or enter *this* agent at all?
    * ``agents`` — egress. Which *other* agents may this role reach, keyed by
      target name, each an :class:`AgentTargetPolicy`.

    ``skills`` is the third block that lowers this way: which named skills may this
    role reach, keyed by skill name, each a :class:`SkillPolicy` naming the
    disclosure levels it governs. Nothing decides on a ``skill:`` key yet — no
    adapter produces one, and an unlisted skill still falls to ``default_policy``.

    All three lower into synthetic tool keys via :attr:`effective_tools`, which both
    policy engines read, so agent-level rules evaluate through the identical
    decision path as tools with no engine change. Agent keys are closed-world
    (R-AGENT-002): an unlisted ``agent.run`` / ``agent.<via>:<target>`` denies at
    the engine rather than falling to ``default_policy``. Whether a gate fires at
    all is a separate, opt-in signal derived per run from whether the policy
    declares the block (``declares_admission()`` / ``declares_reach()``).
    """

    # frozen: policies are immutable after load (inheritance builds fresh
    # instances, nothing reassigns a field), which is what makes memoizing
    # effective_tools safe. cached_property is a plain descriptor, not a field,
    # so pydantic must leave it alone.
    # extra="forbid": a mistyped field (``contraints:``) would otherwise be
    # dropped in silence, and a dropped fence is fail-open.
    model_config = ConfigDict(
        frozen=True, ignored_types=(cached_property,), extra="forbid"
    )

    version: int = 1
    inherits: list[str] = Field(default_factory=list)
    is_mixin: bool = False
    default_policy: BaseToolPolicy = Field(default_factory=BaseToolPolicy)
    # Applied to *every* tool this role can reach, before the tool's own.
    # Unlike ``default_policy.constraints``, which only reaches tools that fall
    # through to the default. Can only narrow: ``mode: deny`` short-circuits
    # before constraints on both engines.
    constraints: list[str] = Field(default_factory=list)
    tools: dict[str, ToolPolicy] = Field(default_factory=dict)
    consts: dict[str, Any] = Field(default_factory=dict)
    admission: BaseToolPolicy | None = None
    agents: dict[str, AgentTargetPolicy] = Field(default_factory=dict)
    skills: dict[str, SkillPolicy] = Field(default_factory=dict)

    @field_validator("constraints")
    @classmethod
    def _validate_constraint_grammar(cls, value: list[str]) -> list[str]:
        return _parse_all(value)

    @field_validator("tools")
    @classmethod
    def _reject_reserved_tool_names(
        cls, value: dict[str, ToolPolicy], info: ValidationInfo
    ) -> dict[str, ToolPolicy]:
        """Keep the ``agent.*`` and ``skill*:`` key namespaces for agent-level gating.

        An authored tool named ``agent.run`` / ``agent.tool:x`` / ``skill:x`` would
        collide with a lowered agent or skill rule in :attr:`effective_tools` and
        silently shadow (or be shadowed by) it. Reject it at load.

        Skipped when validated under a ``{"resolved": True}`` context: a *resolved*
        policy legitimately carries the lowered keys in ``tools`` (the linker's
        :meth:`resolved` builder puts them there), and it must round-trip back
        through this loader when a modular agent's bundle is compiled from its
        resolved YAML (R-POL-002). The guard is an authoring ergonomic — it only
        needs to fire on hand-written source, not on a machine-resolved artifact."""
        if info.context and info.context.get("resolved"):
            return value
        for name in value:
            if is_agent_key(name) or is_skill_key(name):
                raise ValueError(
                    f"tool name {name!r} is reserved for agent-level gating; "
                    "use the 'admission'/'agents'/'skills' blocks instead"
                )
        return value

    @classmethod
    def resolved(
        cls,
        *,
        default_policy: BaseToolPolicy,
        tools: dict[str, ToolPolicy],
        consts: dict[str, Any],
    ) -> "AgentPolicy":
        """Build a linker-resolved policy directly from folded tool keys.

        The fold composes agent-level blocks into lowered ``agent.*`` keys and
        stores them alongside ordinary tools, so a resolved policy carries them
        in ``tools`` rather than in ``admission``/``agents``: per-via divergence
        across capabilities (a target allowed via one mode, denied via another,
        or granted different constraints per mode) can't always be reverse-lowered
        into a single :class:`AgentTargetPolicy`. The reserved-key guard on
        ``tools`` is an authoring ergonomic that does not apply to this machine
        path, so this bypasses validation via ``model_construct`` — every value is
        an already-validated model instance produced by the fold."""
        return cls.model_construct(
            default_policy=default_policy, tools=dict(tools), consts=dict(consts)
        )

    def lowered_agent_tools(self) -> dict[str, BaseToolPolicy]:
        """Expand ``admission``/``agents`` into synthetic tool entries.

        ``admission`` → ``agent.run``; each ``agents`` target → one entry per
        ``via`` mode (``agent.tool:<name>`` / ``agent.handoff:<name>``). Only the
        *listed* rules are lowered; the fallback for an unlisted target is the
        agent gate's concern, not this map's.

        The target name is canonicalized (:func:`~hexgate.security.naming.canonical_name`)
        so the lowered key matches the one the reach gate derives from the runtime
        target's name — both sides normalize identically, or a padded authored name
        would never match and a policy-allowed handoff would fall to closed-world deny.
        """
        lowered: dict[str, BaseToolPolicy] = {}
        if self.admission is not None:
            lowered[AGENT_RUN_TOOL] = self.admission
        for target, target_policy in self.agents.items():
            # Use the AgentTargetPolicy directly (it is a BaseToolPolicy): a bare
            # rebuild would silently drop any field later added to BaseToolPolicy.
            # via is an extra field the engines ignore.
            for via in target_policy.via:
                lowered[agent_target_key(via, canonical_name(target))] = target_policy
        return lowered

    def lowered_skill_tools(self) -> dict[str, BaseToolPolicy]:
        """Expand ``skills`` into synthetic tool entries, one per ``via`` level.

        Each listed skill yields ``skill:<name>`` / ``skill.resource:<name>`` /
        ``skill.script:<name>`` for the levels its rule governs. Only listed skills
        are lowered; the fallback for an unlisted one is the engine's concern.

        The :class:`SkillPolicy` is reused as the lowered value (it *is* a
        :class:`BaseToolPolicy`) rather than rebuilt: a rebuild would silently drop
        any field later added to the base. ``via`` rides along as an extra the
        engines ignore, exactly as on a lowered agent rule.
        """
        return {
            skill_key(via, name): policy
            for name, policy in self.skills.items()
            for via in policy.via
        }

    @cached_property
    def effective_tools(self) -> dict[str, ToolPolicy]:
        """Authored ``tools`` plus the lowered agent-level and skill entries.

        The single view both engines read (:func:`~hexgate.security.policy.get_tool_policy`
        and the Rego compiler), so a lowered ``agent.*`` key evaluates byte-for-byte
        the same on the pydantic and WASM paths. Memoized: ``get_tool_policy`` reads
        this on every decision, and policies are immutable after load (inheritance
        builds fresh instances), so the merge runs once per policy, not per call.
        """
        lowered = {**self.lowered_agent_tools(), **self.lowered_skill_tools()}
        if not lowered:
            return self.tools
        return {**self.tools, **lowered}
