"""Analyze a linked policy bundle for authoring problems — the lint layer.

The linker (:mod:`hexgate.security.linker`) *raises* :class:`LinkError` for the
unfixable cases (a capability that denies, a conflicting const, ``file_scope`` in
a module). This module runs over a **successfully linked** bundle and reports the
*soft* problems that don't stop composition but are almost always mistakes:

- **dead-grant** — a capability grants a tool a boundary ceiling excludes, so the
  grant never fires.
- **redundant-grant** — two capabilities grant the same tool identically.
- **unknown-tool** / **unknown-arg** — a rule references a tool or arg absent from
  the agent's manifest (drift between policy and code), or an admission, reach or
  skill rule reads an arg its gate doesn't pass. Only checked when a manifest is
  supplied. Severity follows the failure direction: drift that
  leaves the real tool looser than intended (fail-open) is an error, see
  :func:`_drift` and :func:`_resolved_drift`.
- **permissive-default** — the ``default`` role grants something no named role
  grants (:func:`check_default_role_exposure`, over a resolved role map).
- **unknown-guard** / **ambiguous-guard** — a baseline ``guards:`` rule that names a
  guard the manifest doesn't declare, or names one declared more than once
  (:func:`lint_guards`). These run over a resolved ``PolicySet`` + manifest, not the
  module pipeline (guards live in a single-file policy; modules reject the block).
- **guard-divergence** — roles resolve to different guard settings, which the
  agent-level guard stance cannot represent.
- **unknown-skill** — a ``skills:`` rule names a skill the manifest doesn't
  declare. Only checked when a manifest is supplied and lists the skills.
- **unknown-root** — a constraint path that no call ever sets: its root isn't
  ``args`` / ``ctx`` / ``run``, or it dots into the ``role`` / ``tool`` string.
  Manifest-free.

:func:`analyze_policy` is the one entry point over a resolved ``PolicySet``: every
check that applies to it, with the manifest-dependent ones gated on a manifest.

Every :class:`PolicyLint` carries the ``source`` file it attributes to — the same
contract the CLI (`hexgate policy check`) and the dashboard editor both consume.
Deferred (needs a solver): semantic conflicts — empty intersection, always-true /
always-false, subsumption.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from hexgate.security.constraints import (
    FACTS,
    PATH_ROOTS,
    ConstraintParseError,
    iter_arg_refs_negated,
    parse_constraint,
)
from hexgate.security.linker import (
    link_policy_set,
    resolve_for_project,
    resolve_role_map,
)
from hexgate.security.models import (
    AgentPolicy,
    BaseToolPolicy,
    FileToolPolicy,
    ToolPolicy,
    gate_args,
    is_reserved_key,
    is_skill_key,
)
from hexgate.security.modules import (
    DEFAULT_AGENT,
    GRANT_MODES,
    LayerKind,
    LinkError,
    LinkResult,
    ModuleContent,
    ProjectLinkResult,
    RoleMatrix,
)
from hexgate.security.naming import canonical_skill_name
from hexgate.security.network import EGRESS_TOOL_ARGS
from hexgate.security.policy_set import DEFAULT_ROLE_NAME, PolicySet, PolicySetError

if TYPE_CHECKING:  # avoid importing the manifest package eagerly
    from hexgate.manifest.models import AgentManifest

Severity = Literal["error", "warning", "info"]
LintCode = Literal[
    "ambiguous-guard",
    "constraint-erased",
    "dead-grant",
    "guard-divergence",
    "implicit-default",
    "link-error",
    "no-default-role",
    "permissive-default",
    "redundant-grant",
    "unknown-arg",
    "unknown-guard",
    "unknown-root",
    "unknown-skill",
    "unknown-tool",
    "unused-capability",
]
SEVERITY_RANK: dict[Severity, int] = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class PolicyLint:
    """One authoring problem, attributed to the file that caused it.

    ``line`` is ``None`` for now (file-level attribution); line-level lands with
    YAML position tracking in the loader. ``tier`` / ``tool`` are set when known.
    """

    code: LintCode
    severity: Severity
    message: str
    source: str | None = None
    line: int | None = None
    tier: LayerKind | None = None
    tool: str | None = None
    role: str | None = None


def check(
    boundaries: list[ModuleContent],
    capabilities: list[ModuleContent],
    *,
    manifest: AgentManifest | None = None,
) -> list[PolicyLint]:
    """Link + analyze in one call.

    A hard failure (the linker rejecting the bundle, or an invalid resolved
    policy) becomes a single ``error`` lint so a caller reports hard failures
    and soft lints through one uniform list. The except tuple matches what
    ``link_policy_set`` can raise: ``LinkError`` from the fold, and
    ``PolicySetError`` / ``ConstraintParseError`` from validating the resolved
    policy (e.g. an undefined ``consts`` reference).
    """
    try:
        result = link_policy_set(boundaries, capabilities)
    except (LinkError, PolicySetError, ConstraintParseError) as exc:
        return [PolicyLint("link-error", "error", str(exc))]
    lints = analyze(result, boundaries, capabilities, manifest=manifest)
    lints += _module_unknown_roots(boundaries, capabilities)
    return sorted(lints, key=lambda lint: SEVERITY_RANK[lint.severity])


def analyze(
    result: LinkResult,
    boundaries: list[ModuleContent],
    capabilities: list[ModuleContent],
    *,
    manifest: AgentManifest | None = None,
) -> list[PolicyLint]:
    """Soft lints over a successfully-linked bundle, most-severe first.

    Needs the input modules (not just ``result``) to know what each layer
    *declared* versus what survived the fold.
    """
    lints: list[PolicyLint] = []
    lints += _dead_grants(result, capabilities)
    lints += _redundant_grants(capabilities)
    lints += _constraint_erased(capabilities)
    if manifest is not None:
        lints += _drift(boundaries, capabilities, manifest)
    return sorted(lints, key=lambda lint: SEVERITY_RANK[lint.severity])


def check_project(
    boundaries: list[ModuleContent],
    library: list[ModuleContent],
    roles: RoleMatrix | None,
    *,
    manifest: AgentManifest | None = None,
) -> list[PolicyLint]:
    """Resolve a project and lint every role. See :func:`check` for the single-role
    form. A hard failure folds into one ``error`` lint, same contract as ``check``.

    Soft lints (dead-grant, drift, ...) run over the generic (``"*"``) agent
    view — the baseline every agent shares; per-agent soft-lint refinement is a
    follow-up. Hard **link errors** are surfaced for every named agent column too,
    so a named-agent cell importing an unknown capability is visible on ``check``
    (not just rejected at write time / silently fail-closed at serve time).
    """
    try:
        result = resolve_for_project(boundaries, library, roles)
    except (LinkError, PolicySetError, ConstraintParseError) as exc:
        return [PolicyLint("link-error", "error", str(exc))]
    lints = analyze_project(result, boundaries, library, roles, manifest=manifest)
    lints += _named_agent_link_errors(boundaries, library, roles)
    return lints


def _named_agent_link_errors(
    boundaries: list[ModuleContent],
    library: list[ModuleContent],
    roles: RoleMatrix | None,
) -> list[PolicyLint]:
    """A ``link-error`` lint per named-agent column that doesn't resolve.

    ``check_project`` resolves the ``"*"`` column for its soft lints; this covers
    the hard-error case for named columns (an unknown-capability import in
    ``roles: {member: {billing_bot: [nope]}}``), which ``"*"`` never touches."""
    if not isinstance(roles, Mapping):
        return []
    named = {
        agent
        for cells in roles.values()
        if isinstance(cells, Mapping)
        for agent in cells
    } - {DEFAULT_AGENT}
    lints: list[PolicyLint] = []
    for agent in sorted(named):
        try:
            resolve_for_project(boundaries, library, roles, agent=agent)
        except (LinkError, PolicySetError, ConstraintParseError) as exc:
            lints.append(PolicyLint("link-error", "error", f"agent {agent!r}: {exc}"))
    return lints


def analyze_project(
    result: ProjectLinkResult,
    boundaries: list[ModuleContent],
    library: list[ModuleContent],
    roles: RoleMatrix | None,
    *,
    manifest: AgentManifest | None = None,
) -> list[PolicyLint]:
    """Soft lints across every role, each tagged with the role it fired in.

    A grant dead under one role's ceiling can be alive under another, so the
    per-capability lints run once per role over that role's imported set (for the
    generic ``"*"`` agent view). Two project-level lints span roles:
    ``unused-capability`` (a library pack no role/agent imports) and
    ``no-default-role`` (roles defined but no ``default``, so unroled callers get
    fail-closed deny).
    """
    # Same expansion the resolver used, so the analyzer lints exactly the roles
    # that compiled. Raises LinkError on an unknown capability, matching
    # resolve_for_project — but check_project resolves first, so by the time we
    # get here the same input has already succeeded.
    resolved = resolve_role_map(roles, library)

    lints: list[PolicyLint] = []
    for role, caps in resolved.items():
        role_result = result.by_role.get(role)
        if role_result is None:
            continue
        for lint in analyze(role_result, boundaries, caps, manifest=manifest):
            lints.append(replace(lint, role=role))

    # Once per module, not per role: a module's paths don't depend on the role.
    lints += _module_unknown_roots(boundaries, library)
    lints += _unused_capabilities(library, _all_imported_names(roles, library))
    if roles and DEFAULT_ROLE_NAME not in roles:
        lints.append(
            PolicyLint(
                code="no-default-role",
                severity="info",
                message=(
                    f"no {DEFAULT_ROLE_NAME!r} role defined; a caller with no role "
                    "resolves to fail-closed deny"
                ),
                # role stays None: this spans roles, so a role-scoped `check
                # --role X` view must still surface it (like unused-capability).
            )
        )
    return sorted(lints, key=lambda lint: SEVERITY_RANK[lint.severity])


def _all_imported_names(
    roles: RoleMatrix | None, library: list[ModuleContent]
) -> set[str]:
    """Capability names imported by ANY ``(role, agent)`` cell.

    Spans the whole matrix (every agent column, not just ``"*"``) so a capability
    used only by a named agent isn't falsely flagged unused. ``roles is None`` is
    the all-compose case: every library capability counts as imported.
    """
    if roles is None:
        return {cap.name for cap in library}
    names: set[str] = set()
    for cells in roles.values():
        if isinstance(cells, Mapping):  # the (role, agent) matrix
            for binding in cells.values():
                names.update(binding.capabilities)
        else:  # legacy flat `role: [names]`
            names.update(cells)
    return names


def _unused_capabilities(
    library: list[ModuleContent], imported: set[str]
) -> list[PolicyLint]:
    """A library capability that no role/agent imports. Authoring dead-weight."""
    return [
        PolicyLint(
            code="unused-capability",
            severity="info",
            message=f"capability {cap.name!r} is imported by no role",
            source=cap.source,
            tier="capability",
            tool=None,
        )
        for cap in library
        if cap.name not in imported
    ]


def _dead_grants(
    result: LinkResult, capabilities: list[ModuleContent]
) -> list[PolicyLint]:
    """A capability grant that the effective policy doesn't allow never fires.

    Keyed off the *resolved* policy, not just ``trace.shadowed``, so it catches
    every dead grant: a ceiling that excludes the tool AND a boundary that
    hard-denies it (the latter never enters ``shadowed`` — it takes the
    absolute-deny path in the fold). A grant survives iff the effective tool is
    still allow/approval.
    """
    effective = result.effective[DEFAULT_ROLE_NAME]
    out: list[PolicyLint] = []
    for cap in capabilities:
        # effective_tools: composed agent-level grants (admission/reach lowered to
        # agent.* keys) are linted like ordinary tools. A shadowed agent key stays
        # in the resolved policy as an explicit deny (not GRANT_MODES), so it is
        # correctly reported dead here.
        for tool, tp in cap.policy.effective_tools.items():
            if tp.mode not in GRANT_MODES:
                continue
            eff = effective.tools.get(tool)
            if eff is not None and eff.mode in GRANT_MODES:
                continue  # the grant contributes to the effective allow — alive
            out.append(
                PolicyLint(
                    code="dead-grant",
                    severity="warning",
                    message=(
                        f"{cap.name!r} grants {tool!r} but {_dead_reason(tool, result)}"
                        f" — this grant never fires"
                    ),
                    source=cap.source,
                    tier="capability",
                    tool=tool,
                )
            )
    return out


def _dead_reason(tool: str, result: LinkResult) -> str:
    """Why a grant is dead: a ceiling that excludes it, or a boundary deny."""
    shadowed_by = result.trace.shadowed.get(tool)
    if shadowed_by is not None:
        return f"boundary {shadowed_by.module!r} (a ceiling) never permits it"
    return "a boundary denies it"


def _redundant_grants(capabilities: list[ModuleContent]) -> list[PolicyLint]:
    """Two capabilities granting the same tool with the same mode + constraints."""
    out: list[PolicyLint] = []
    seen: dict[tuple[str, str, tuple[str, ...]], ModuleContent] = {}
    for cap in capabilities:
        for tool, tp in cap.policy.effective_tools.items():
            if tp.mode not in GRANT_MODES:
                continue
            key = (tool, tp.mode, tuple(sorted(tp.constraints)))
            first = seen.get(key)
            if first is not None:
                out.append(
                    PolicyLint(
                        code="redundant-grant",
                        severity="info",
                        message=(
                            f"{cap.name!r} repeats the {tool!r} grant already in "
                            f"{first.name!r}"
                        ),
                        source=cap.source,
                        tier="capability",
                        tool=tool,
                    )
                )
            else:
                seen[key] = cap
    return out


def _constraint_erased(capabilities: list[ModuleContent]) -> list[PolicyLint]:
    """A constrained grant nullified by an unconditional sibling grant.

    Capability grants for one tool union, so an unconditional grant (no
    constraints) widens the tool to everything and drops every sibling's
    condition. That is intended union semantics, but it is the security-relevant
    direction (a tight rule silently erased), so it warrants a warning.
    """
    grants: dict[str, list[tuple[ModuleContent, Any]]] = {}
    for cap in capabilities:
        for tool, tp in cap.policy.effective_tools.items():
            if tp.mode in GRANT_MODES:
                grants.setdefault(tool, []).append((cap, tp))

    out: list[PolicyLint] = []
    for tool, entries in grants.items():
        unconditional = [cap for cap, tp in entries if not tp.constraints]
        if not unconditional:
            continue
        for cap, tp in entries:
            if tp.constraints:
                out.append(
                    PolicyLint(
                        code="constraint-erased",
                        severity="warning",
                        message=(
                            f"{cap.name!r} constrains {tool!r}, but "
                            f"{unconditional[0].name!r} grants it unconditionally, "
                            f"so the constraint is dropped from the effective policy"
                        ),
                        source=cap.source,
                        tier="capability",
                        tool=tool,
                    )
                )
    return out


def _drift(
    boundaries: list[ModuleContent],
    capabilities: list[ModuleContent],
    manifest: AgentManifest,
) -> list[PolicyLint]:
    """Rules referencing tools / args / skills the agent's code doesn't have.

    Severity follows the failure direction, not just the tier:
      * boundary allow/approval (a ceiling) naming a missing tool leaves the real
        tool uncapped -> fail-open -> error.
      * boundary deny naming a missing tool protects nothing and breaks nothing
        -> info.
      * capability drift is a dead grant -> warning.
      * an arg typo compares False, so it fails closed (warning) unless it ends
        up under an odd number of ``not`` -- counting the ``not (...)`` the linker
        wraps a boundary deny in -- where it fails open (error).

    Policy-level ``constraints`` are not walked: they have no tool to check
    ``args.*`` against, and ``link()`` rejects the field in a module anyway, so
    this pipeline never sees one.
    """
    tool_props = _tool_props(manifest)
    skills = _manifest_skills(manifest)
    out: list[PolicyLint] = []
    for module, tier in _tiered(boundaries, capabilities):
        rules = module.policy.effective_tools
        owner, source = repr(module.name), module.source
        out += _tool_drift(rules, tool_props, owner=owner, source=source, tier=tier)
        out += _skill_drift(rules, skills, owner=owner, source=source, tier=tier)
    return out


def _tiered(
    boundaries: list[ModuleContent], capabilities: list[ModuleContent]
) -> Iterator[tuple[ModuleContent, LayerKind]]:
    """Each module with its tier, boundaries first."""
    yield from ((module, "boundary") for module in boundaries)
    yield from ((module, "capability") for module in capabilities)


def _runs_negated(tier: LayerKind | None, mode: str) -> bool:
    """Whether a module rule's constraints run under ``not``: the linker folds
    a boundary deny's region into ``not (...)``."""
    return tier == "boundary" and mode == "deny"


def _unknown_tool_severity(tier: LayerKind, mode: str) -> Severity:
    """A boundary deny on a missing tool is harmless; a boundary ceiling that
    names a missing tool leaves the real tool uncapped (fail-open)."""
    if tier == "capability":
        return "warning"
    return "info" if mode == "deny" else "error"


def lint_guards(
    policy_set: PolicySet,
    manifest: AgentManifest,
    *,
    source: str | None = None,
) -> list[PolicyLint]:
    """Authoring lints for the ``guards:`` block against the agent's manifest.

    The ergonomic ahead of the runtime "stop cold" (R-GUARD-006 / R-GUARD-007):
    surface, at ``hexgate policy validate`` time, the guard mistakes that otherwise
    only bite at construction or run silently. Severity mirrors the runtime
    consequence, as ``_drift`` does — a guaranteed hard-stop is an ``error``, a silent
    no-op is a ``warning``:

    - ``unknown-guard`` (**error**) — a baseline ``guards:`` rule names a guard the
      manifest does not declare (a typo, or a guard deleted from the code). The runtime
      stops cold on this, so the policy is guaranteed to crash the agent; failing
      validate is faithful.
    - ``ambiguous-guard`` (**error**) — a governed name is declared by more than one
      guard (two functions share a ``__name__``), so a policy cannot address them
      separately. The runtime also stops cold (``GuardClosedWorldError`` "attached
      more than once").

    v1 governs guards only at the baseline (R-GUARD-006), so there is no per-tool
    reach lint. Runs on the resolved ``policy_set`` (guards live in a single-file
    policy; modules reject the block), across every role, deduped. Guard names are
    matched by ``GuardManifest.name`` (the guard's function label), the same key the
    runtime and the policy use.
    """
    by_name: dict[str, list] = {}
    for gm in manifest.guards or []:
        by_name.setdefault(gm.name, []).append(gm)

    # Every baseline guard reference across roles, deduped. Iterating roles (not
    # guard_stance) keeps this robust to a role-divergent policy, which is a separate
    # build-time error.
    baseline: set[str] = set()
    for role in policy_set.roles:
        baseline.update(policy_set.policy_for(role).guards)

    out: list[PolicyLint] = []
    for name in sorted(baseline):
        entries = by_name.get(name, ())
        if not entries:
            out.append(
                PolicyLint(
                    code="unknown-guard",
                    severity="error",
                    message=(
                        f"policy governs guard {name!r}, which the agent's manifest "
                        "doesn't declare (a typo, or a guard removed from the code); "
                        "the agent would stop cold at construction"
                    ),
                    source=source,
                )
            )
        elif len(entries) > 1:
            out.append(
                PolicyLint(
                    code="ambiguous-guard",
                    severity="error",
                    message=(
                        f"policy governs guard {name!r}, which is attached more than "
                        "once, so a policy cannot address them separately; the agent "
                        "would stop cold at construction — give each a distinct name"
                    ),
                    source=source,
                )
            )
    return out


def _tool_props(manifest: AgentManifest) -> dict[str, set[str]]:
    """Accepted argument names by tool: each manifest tool's, plus the egress
    tools', which are enforced like tools but never appear in a manifest."""
    props = {name: set(args) for name, args in EGRESS_TOOL_ARGS.items()}
    props.update({t.name: set(t.input_schema.properties) for t in manifest.tools})
    return props


def _tool_drift(
    tools: dict[str, ToolPolicy],
    tool_props: dict[str, set[str]],
    *,
    owner: str,
    source: str | None,
    tier: LayerKind | None,
    default: BaseToolPolicy | None = None,
    role: str | None = None,
) -> list[PolicyLint]:
    """``unknown-tool`` / ``unknown-arg`` for one rule set: a module's (``tier``
    set) or a resolved role's (``default``, its fallback policy, set). ``owner``
    names what holds the rules in the messages. A lowered agent or skill key is
    never an unknown tool (:func:`_skill_drift` checks the skill name); its args
    are checked against what its gate passes."""
    out: list[PolicyLint] = []
    for tool, tp in tools.items():
        if is_reserved_key(tool):
            valid_args = gate_args(tool)
        elif tool in tool_props:
            valid_args = tool_props[tool]
        else:
            severity = (
                _resolved_unknown_tool_severity(tp, default)
                if default is not None
                else _unknown_tool_severity(tier, tp.mode)
            )
            out.append(
                PolicyLint(
                    code="unknown-tool",
                    severity=severity,
                    message=(
                        f"{owner} references tool {tool!r}, which the agent's "
                        "manifest doesn't declare"
                    ),
                    source=source,
                    tier=tier,
                    tool=tool,
                    role=role,
                )
            )
            continue
        if default is not None and tp.mode == "deny":
            continue  # a resolved deny is unconditional: its constraints never run
        negated = _runs_negated(tier, tp.mode)
        unknown = _unknown_args(tp.constraints, valid_args, negated)
        for arg, severity in unknown.items():
            out.append(
                PolicyLint(
                    code="unknown-arg",
                    severity=severity,
                    message=(
                        f"{owner} constrains {tool!r} on args.{arg}, "
                        "which the tool doesn't accept"
                    ),
                    source=source,
                    tier=tier,
                    tool=tool,
                    role=role,
                )
            )
    return out


def _manifest_skills(manifest: AgentManifest) -> set[str] | None:
    """The skills the agent has, named as the skill gate's keys name them, or
    ``None`` when the manifest doesn't list them: the builders record ``None``
    both for no skills and for a listing that failed, while the skill gate
    still runs."""
    if manifest.skills is None:
        return None
    return {canonical_skill_name(s.name) for s in manifest.skills}


def _skill_drift(
    tools: Mapping[str, ToolPolicy],
    skills: set[str] | None,
    *,
    owner: str,
    source: str | None,
    tier: LayerKind | None,
    role: str | None = None,
) -> list[PolicyLint]:
    """``unknown-skill`` for each skill the lowered ``skill*:`` keys in ``tools``
    name that ``skills`` lacks, once per skill over its levels.

    In a resolved role (``tier`` unset) the real skill, unlisted, is denied
    closed-world either way: a grant never fires (warning), a deny protects
    nothing (info). A module's is graded like an unknown tool's
    (:func:`_unknown_tool_severity`): a boundary grant's fence never reaches the
    real skill, which runs on a capability's grant alone (error)."""
    if skills is None:
        return []
    worst: dict[str, Severity] = {}
    for key, tp in tools.items():
        if not is_skill_key(key):
            continue
        name = key.partition(":")[2]
        if name not in skills:
            _keep_worst(worst, name, _unknown_skill_severity(tier, tp.mode))
    return [
        PolicyLint(
            code="unknown-skill",
            severity=severity,
            message=(
                f"{owner} governs skill {name!r}, which the agent's manifest "
                "doesn't declare"
            ),
            source=source,
            tier=tier,
            role=role,
        )
        for name, severity in sorted(worst.items())
    ]


def _unknown_skill_severity(tier: LayerKind | None, mode: str) -> Severity:
    """See :func:`_skill_drift`."""
    if tier is not None:
        return _unknown_tool_severity(tier, mode)
    return "info" if mode == "deny" else "warning"


# How far each mode restricts a call, loosest first.
_STRICTNESS = {"allow": 0, "approval_required": 1, "deny": 2}


def _resolved_unknown_tool_severity(
    rule: ToolPolicy, default: BaseToolPolicy
) -> Severity:
    """The real tool falls through to ``default``. If that lets through any call
    the misspelled rule would have denied or sent to approval -- a tighter mode,
    or a restricted grant over a non-deny default -- the real tool runs looser
    than intended: fail-open, error. Otherwise a deny protects nothing (info) and
    a grant never fires (warning)."""
    if _STRICTNESS[rule.mode] > _STRICTNESS[default.mode] or (
        rule.mode != "deny" and default.mode != "deny" and _restricts_calls(rule)
    ):
        return "error"
    return "info" if rule.mode == "deny" else "warning"


def _restricts_calls(rule: ToolPolicy) -> bool:
    """Whether a grant denies some calls: :func:`evaluate_tool_call` checks its
    ``constraints`` and, on a file tool, its ``file_scope``."""
    return bool(rule.constraints) or (
        isinstance(rule, FileToolPolicy) and rule.file_scope is not None
    )


def _unknown_args(
    constraints: list[str], valid_args: Collection[str], negated: bool
) -> dict[str, Severity]:
    """Each ``args.<x>`` the constraints use that isn't in ``valid_args``, with
    its worst severity over every use (see :func:`_missing_refs`)."""

    def unknown(path: tuple[str, ...]) -> str | None:
        if len(path) < 2 or path[0] != "args" or path[1] in valid_args:
            return None
        return path[1]

    return _missing_refs(constraints, negated, unknown)


def _missing_refs(
    constraints: list[str],
    negated: bool,
    missing: Callable[[tuple[str, ...]], str | None],
) -> dict[str, Severity]:
    """Each field path the constraints use that is never set at runtime, keyed by
    what ``missing`` names it (``None``: the path is fine), with its worst
    severity over every use.

    A comparison on a missing field is False. Under an even number of ``not``
    (counting ``negated``, the rule's own) that fails closed (warning); under an
    odd number it is always True, which fails open (error)."""
    out: dict[str, Severity] = {}
    for raw in constraints:
        for path, odd in iter_arg_refs_negated(parse_constraint(raw), negated):
            name = missing(path)
            if name is not None:
                _keep_worst(out, name, "error" if odd else "warning")
    return out


def _keep_worst(worst: dict[Any, Severity], key: Any, severity: Severity) -> None:
    """Record ``severity`` under ``key`` unless a more severe one is there."""
    current = worst.get(key)
    if current is None or SEVERITY_RANK[severity] < SEVERITY_RANK[current]:
        worst[key] = severity


# ---------------------------------------------------------------------------
# Cross-role exposure. Not part of ``analyze()``: that pipeline is module-scoped
# and single-role (``LinkResult.effective`` holds only ``default``), so it has
# nothing to say about a role map. Takes a resolved PolicySet instead.
# ---------------------------------------------------------------------------


def _exposed_grant_message(tool: str, mode: str, alias: str | None) -> str:
    """Wording for one grant reachable through the fallback role.

    Names the aliased role when the fallback is inferred: saying "no named role
    grants it" would be false there, since the alias *is* a named role.
    """
    if alias is not None:
        return (
            f"{alias!r} is the inferred fallback and grants {tool!r} ({mode}), so "
            "any caller reaches it by carrying a role this policy doesn't "
            f"define. Add an explicit least-privilege {DEFAULT_ROLE_NAME!r} role, "
            f"or move the grant into a mixin the roles that need it inherit."
        )
    return (
        f"the {DEFAULT_ROLE_NAME!r} role grants {tool!r} ({mode}) and no named "
        f"role does. {DEFAULT_ROLE_NAME!r} is the fallback for every unrecognised "
        "role name, so any caller can reach this tool by carrying a role this "
        "policy doesn't define. Move the grant to the roles that need it, or "
        f"into a mixin they inherit, and keep {DEFAULT_ROLE_NAME!r} "
        "least-privilege."
    )


def check_default_role_exposure(
    policy_set: PolicySet, *, source: str | None = None
) -> list[PolicyLint]:
    """Warn when the ``default`` role grants something no named role grants.

    Any unrecognised role name resolves to ``default`` and joins the caller's
    union, so a tool reachable only through ``default`` is reachable by anyone.

    A document that declares roles but no ``default`` also gets
    ``implicit-default``, and its per-grant messages name the aliased role rather
    than claiming no named role grants them.

    Silent for a single-role policy set: a legacy flat ``policy.yaml`` *is* the
    ``default`` role. ``warning`` rather than ``error`` for the same reason —
    CI opts in via ``--max-severity warning``.
    """
    named = [role for role in policy_set.roles if role != DEFAULT_ROLE_NAME]
    if not named:
        return []

    default_policy = policy_set.policy_for(DEFAULT_ROLE_NAME)
    # Drop the role ``default`` aliases — inferred by the loader, or named by an
    # explicit ``default=``. It resolves to the *same policy object*, so leaving
    # it in answers "does a named role grant this too?" with yes for every one of
    # its own grants, silencing the check on the shape that most needs it.
    others = [
        policy
        for policy in (policy_set.policy_for(role) for role in named)
        if policy is not default_policy
    ]
    alias = policy_set.aliased_default
    lints: list[PolicyLint] = []

    if alias is not None:
        lints.append(
            PolicyLint(
                code="implicit-default",
                severity="warning",
                message=(
                    f"no role is named {DEFAULT_ROLE_NAME!r}, so {alias!r} is the "
                    "fallback for every role name this policy doesn't define — "
                    "any caller reaches its grants by carrying an undefined "
                    f"name. Add an explicit least-privilege "
                    f"{DEFAULT_ROLE_NAME!r} role."
                ),
                source=source,
            )
        )

    # effective_tools, not tools: a default role that grants an admission or
    # agents rule (lowered to an ``agent.*`` key) is reachable by any undefined
    # role name too, and that is exactly the exposure this lint exists to catch.
    for tool, tool_policy in sorted(default_policy.effective_tools.items()):
        if tool_policy.mode not in GRANT_MODES:
            continue
        if any(
            tool in other.effective_tools
            and other.effective_tools[tool].mode in GRANT_MODES
            for other in others
        ):
            continue
        lints.append(
            PolicyLint(
                code="permissive-default",
                severity="warning",
                message=_exposed_grant_message(tool, tool_policy.mode, alias),
                source=source,
                tool=tool,
            )
        )

    if default_policy.default_policy.mode in GRANT_MODES and not any(
        other.default_policy.mode in GRANT_MODES for other in others
    ):
        lints.append(
            PolicyLint(
                code="permissive-default",
                severity="warning",
                message=(
                    f"the {DEFAULT_ROLE_NAME!r} role's default_policy is "
                    f"{default_policy.default_policy.mode!r}, so every tool not "
                    "listed anywhere is reachable by any caller carrying an "
                    f"unrecognised role name. Set it to 'deny' and grant tools "
                    "explicitly."
                ),
                source=source,
            )
        )
    return lints


# ---------------------------------------------------------------------------
# The one entry point over a resolved policy set (R-POL-003): every caller that
# reports lints on one goes through it, so a check added here reaches them all.
# ---------------------------------------------------------------------------


def analyze_policy(
    policy_set: PolicySet,
    *,
    manifest: AgentManifest | None = None,
    source: str | None = None,
) -> list[PolicyLint]:
    """Every check over a resolved policy set, most-severe first.

    Manifest-free: ``guard-divergence``, the ``default``-role exposure lints and
    ``unknown-root``. With ``manifest``: ``unknown-guard`` / ``ambiguous-guard``
    and the ``unknown-tool`` / ``unknown-arg`` / ``unknown-skill`` drift.
    ``source`` attributes the findings to the file the policy came from.

    The drift runs on the resolved form, so it suits a single-file policy. A
    module-built one keeps :func:`check` / :func:`check_project`: linking drops a
    boundary fence on a misspelled tool, leaving the real tool uncapped with
    nothing left here to flag.
    """
    lints = _guard_divergence(policy_set, source=source)
    lints += check_default_role_exposure(policy_set, source=source)
    lints += _unknown_roots(policy_set, source=source)
    if manifest is not None:
        lints += lint_guards(policy_set, manifest, source=source)
        lints += _resolved_drift(policy_set, manifest, source=source)
    return sorted(lints, key=lambda lint: SEVERITY_RANK[lint.severity])


def _guard_divergence(policy_set: PolicySet, *, source: str | None) -> list[PolicyLint]:
    """Roles that set different guard settings. The runtime builds one guard
    pipeline per agent, so building a guarded agent from this policy raises."""
    try:
        policy_set.guard_stance()
    except PolicySetError as exc:
        return [PolicyLint("guard-divergence", "error", str(exc), source=source)]
    return []


def _resolved_drift(
    policy_set: PolicySet,
    manifest: AgentManifest,
    *,
    source: str | None,
) -> list[PolicyLint]:
    """Tools, arguments and skills each resolved role names that the manifest
    lacks.

    The resolved form of :func:`_drift`. A rule on a missing tool is graded
    against the role's default, which the real tool falls through to (see
    :func:`_resolved_unknown_tool_severity`). A resolved deny is unconditional,
    so its constraints never run and are not checked; a grant's arg typo fails
    closed, or open under ``not`` (see :func:`_unknown_args`). An aliased
    ``default`` is reported under the role it aliases. Constraints that span
    tools are checked by :func:`_shared_constraint_drift`.
    """
    tool_props = _tool_props(manifest)
    skills = _manifest_skills(manifest)
    out: list[PolicyLint] = []
    for role in _reported_roles(policy_set):
        policy = policy_set.policy_for(role)
        rules, owner = policy.effective_tools, f"role {role!r}"
        out += _tool_drift(
            rules,
            tool_props,
            owner=owner,
            source=source,
            tier=None,
            default=policy.default_policy,
            role=role,
        )
        out += _skill_drift(
            rules, skills, owner=owner, source=source, tier=None, role=role
        )
    return out + _shared_constraint_drift(policy_set, tool_props, source=source)


def _granted_gate_args(policy: AgentPolicy) -> set[str]:
    """The args of the gate decisions ``policy`` can let through, which its
    policy-level constraints also run on. An unlisted reserved key is denied
    closed-world, and a deny never runs constraints, so only listed grants count."""
    return set().union(
        *(
            gate_args(key)
            for key, rule in policy.effective_tools.items()
            if is_reserved_key(key) and rule.mode != "deny"
        )
    )


def _reported_roles(policy_set: PolicySet) -> list[str]:
    """The roles to attribute lints to. ``default`` can be the very object of a
    named role -- inferred by the loader, or named by an explicit ``default=`` --
    and is then left out, so its findings carry the name the author wrote."""
    default_policy = policy_set.policy_for(DEFAULT_ROLE_NAME)
    default_is_alias = any(
        policy_set.policy_for(role) is default_policy
        for role in policy_set.roles
        if role != DEFAULT_ROLE_NAME
    )
    return [
        role
        for role in policy_set.roles
        if not (role == DEFAULT_ROLE_NAME and default_is_alias)
    ]


def _shared_constraint_drift(
    policy_set: PolicySet,
    tool_props: dict[str, set[str]],
    *,
    source: str | None,
) -> list[PolicyLint]:
    """``args.<x>`` typos in constraints that span tools: the policy-level ones
    (every call) and ``default_policy``'s (every tool the role doesn't list).

    ``<x>`` is a typo only when no tool the constraint applies to accepts it;
    an arg some tools lack is the author's own fence on the others. Each arg is
    reported once, at its worst severity, naming the roles that carry it -- or
    none when every role does, as a file-level constraint is copied into each.
    """
    tool_args = set().union(*tool_props.values())
    roles = _reported_roles(policy_set)
    worst: dict[tuple[str, str], Severity] = {}
    carriers: dict[tuple[str, str], list[str]] = {}
    for role in roles:
        policy = policy_set.policy_for(role)
        every_arg = tool_args | _granted_gate_args(policy)
        scopes = [("policy-level", policy.constraints, every_arg)]
        fallthrough = [
            args
            for tool, args in tool_props.items()
            if tool not in policy.effective_tools
        ]
        # A deny default never evaluates its constraints.
        if fallthrough and policy.default_policy.mode != "deny":
            scopes.append(
                (
                    "default_policy",
                    policy.default_policy.constraints,
                    set().union(*fallthrough),
                )
            )
        for kind, constraints, valid in scopes:
            for arg, severity in _unknown_args(constraints, valid, False).items():
                _keep_worst(worst, (kind, arg), severity)
                carriers.setdefault((kind, arg), []).append(role)
    out: list[PolicyLint] = []
    for (kind, arg), severity in sorted(worst.items()):
        named = carriers[(kind, arg)]
        everywhere = len(named) == len(roles)
        where = "" if everywhere else " in " + ", ".join(f"role {r!r}" for r in named)
        out.append(
            PolicyLint(
                code="unknown-arg",
                severity=severity,
                message=(
                    f"a {kind} constraint{where} uses args.{arg}, which no tool "
                    "it applies to accepts"
                ),
                source=source,
                role=named[0] if len(named) == 1 and not everywhere else None,
            )
        )
    return out


def _unknown_roots(policy_set: PolicySet, *, source: str | None) -> list[PolicyLint]:
    """Constraint paths no call ever sets, such as ``user.department`` or
    ``role.name``: the constraint loads, then the path is always missing.

    Every constraint that runs is checked: policy-level, ``default_policy``'s and
    each rule's, a deny's excepted. Each path is reported once, at its worst
    severity (see :func:`_missing_refs`), naming the roles that carry it -- or
    none when every role does, as :func:`_shared_constraint_drift` does."""
    roles = _reported_roles(policy_set)
    worst: dict[str, Severity] = {}
    carriers: dict[str, list[str]] = {}
    for role in roles:
        live = _live_constraints(policy_set.policy_for(role))
        for path, severity in _unset_paths([(live, False)]).items():
            _keep_worst(worst, path, severity)
            carriers.setdefault(path, []).append(role)
    out: list[PolicyLint] = []
    for path, severity in sorted(worst.items()):
        named = carriers[path]
        everywhere = len(named) == len(roles)
        where = "" if everywhere else " in " + ", ".join(f"role {r!r}" for r in named)
        role = named[0] if len(named) == 1 and not everywhere else None
        out.append(
            _unknown_root_lint(path, severity, where=where, role=role, source=source)
        )
    return out


def _module_unknown_roots(
    boundaries: list[ModuleContent], capabilities: list[ModuleContent]
) -> list[PolicyLint]:
    """The module form of :func:`_unknown_roots`, attributed to each module.
    Modules reject policy-level and ``default_policy`` constraints, so only
    rules carry any; a boundary deny's run inside the ``not (...)`` the linker
    wraps it in."""
    out: list[PolicyLint] = []
    for module, tier in _tiered(boundaries, capabilities):
        worst = _unset_paths(
            (tp.constraints, _runs_negated(tier, tp.mode))
            for tp in module.policy.effective_tools.values()
        )
        out += [
            _unknown_root_lint(path, severity, source=module.source, tier=tier)
            for path, severity in sorted(worst.items())
        ]
    return out


def _unset_paths(groups: Iterable[tuple[list[str], bool]]) -> dict[str, Severity]:
    """Each path no call sets over ``(constraints, negated)`` groups, at its
    worst severity."""
    worst: dict[str, Severity] = {}
    for constraints, negated in groups:
        for path, severity in _missing_refs(constraints, negated, _unset_path).items():
            _keep_worst(worst, path, severity)
    return worst


def _unknown_root_lint(
    path: str,
    severity: Severity,
    *,
    source: str | None,
    where: str = "",
    role: str | None = None,
    tier: LayerKind | None = None,
) -> PolicyLint:
    """One ``unknown-root``. A ``consts.<name>`` path is valid as a comparison
    operand but reaches here from ``count()`` / ``every()`` / ``any()``, which
    read a field path, never a constant, so it gets its own advice."""
    if path.startswith("consts."):
        why = (
            "count(), every() and any() read a field path, not a constant; "
            "compare against the constant instead, or inline its list"
        )
    else:
        why = (
            "no call sets it. A path starts with args., ctx. or run., and "
            "role and tool are plain strings"
        )
    return PolicyLint(
        code="unknown-root",
        severity=severity,
        message=f"a constraint{where} reads {path}: {why}",
        source=source,
        tier=tier,
        role=role,
    )


def _live_constraints(policy: AgentPolicy) -> list[str]:
    """The constraints that can run: policy-level, and every rule's but a deny's."""
    rules = [policy.default_policy, *policy.effective_tools.values()]
    return policy.constraints + [
        c for rule in rules if rule.mode != "deny" for c in rule.constraints
    ]


def _unset_path(path: tuple[str, ...]) -> str | None:
    """``path`` dotted, if no call sets it. A lone identifier reaches here as
    a fact, or as the collection of ``count()`` / ``every()`` / ``any()``."""
    if path[0] in PATH_ROOTS or (len(path) == 1 and path[0] in FACTS):
        return None
    return ".".join(path)
