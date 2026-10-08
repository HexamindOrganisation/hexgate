"""Role-aware policy bundles — load, resolve inheritance, pick by role.

A single ``policy.yaml`` is the legacy shape (one policy applies to every
caller). A ``policies/`` directory is the new shape:

    agent/
    ├── agent.yaml
    ├── system.md
    └── policies/
        ├── default.yaml      # fallback when the caller's roles are unknown
        ├── read_only.yaml    # mixin — is_mixin: true
        ├── support.yaml      # inherits: [read_only]
        └── billing.yaml      # inherits: [read_only, support]

A :class:`PolicySet` holds the resolved (post-inheritance) policy per role
name, plus the ``default`` fallback. The enforcer calls ``policy_for(role)``
once per role the caller carries; this module stays single-role.

``default`` is a *fallback*, not a *floor*: a caller whose role is defined never
inherits it. For a baseline shared by every role, use a mixin and ``inherits``.

Inheritance semantics: left-to-right merge — ``inherits: [A, B]`` resolves
to ``merge(A, merge(B, self))``, where ``merge`` deep-merges the ``tools``
maps (child entries override parent entries by tool name) and replaces
scalar fields (``default_policy``) with the child's value when set.
Policy-level ``constraints`` are the exception: they union across the chain.

Mixin policies (``is_mixin: true``) can only be referenced via ``inherits``
— they're never picked as the effective policy for any context scope.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import yaml

from hexgate.runtime.agent_usage import AGENT_USAGE_VOCABULARY, KNOWN_AGENT_USAGE_PATHS
from hexgate.runtime.run_facts import LIST_PATHS, SCALAR_PATHS
from hexgate.security.constraints import (
    LEFT,
    Node,
    Ref,
    iter_arg_refs,
    iter_cmp_operands,
    iter_const_refs,
    parse_constraint,
)
from hexgate.security.decision import Verdict
from hexgate.security.models import (
    AGENT_RUN_TOOL,
    AgentPolicy,
    AgentTargetPolicy,
    BaseToolPolicy,
    GuardRule,
    SkillPolicy,
    ToolPolicy,
    is_agent_reach_key,
    is_agent_via_key,
    is_skill_key,
)

DEFAULT_ROLE_NAME = "default"

# Top-level flag the resolve serializer stamps onto a resolved (already-lowered)
# policy document, so ``load_policy_set_from_dict`` loads it under a "resolved"
# context that accepts lowered ``agent.*`` keys in ``tools`` (R-POL-002 compiles a
# modular agent's bundle from this re-parsed YAML). Hand-written policies omit it,
# so the reserved-name guard still fires on authored source.
RESOLVED_POLICY_MARKER = "_resolved"

# Sentinel for the memoized guard stance: distinguishes "not computed" from a
# genuinely computed None (no role configures a guard).
_UNSET: object = object()

_ROLES_KEY = "roles"
_CONSTRAINTS_KEY = "constraints"
_VERSION_KEY = "version"

# Legal siblings of ``roles:``. Anything else is rejected, not ignored: that
# shape validates only what sits under ``roles:``, so a stray key would vanish.
_FILE_LEVEL_KEYS = frozenset(
    {_ROLES_KEY, _CONSTRAINTS_KEY, _VERSION_KEY, RESOLVED_POLICY_MARKER}
)

_ROOTED_PATH_SEGMENTS = 2  # run.* and agent_usage.* are flat: <root>.<name>
_ORDERED_OPS = frozenset({"<", "<=", ">", ">="})
# ==/!= excluded: list equality is well-defined, just not useful here.
_SCALAR_ONLY_OPS = _ORDERED_OPS | {"in", "not in"}


class PolicySetError(ValueError):
    """Raised when a role's policies/ directory is malformed."""


class PolicySet:
    """Resolved role-to-policy map for one agent.

    ``policies`` keys are role names; values are fully-resolved
    :class:`AgentPolicy` instances (inheritance flattened, mixins inlined).
    The ``default`` key is always present — it's what a lookup falls back to
    when a role is ``None`` or doesn't match any defined role.

    ``aliased_default`` names the role a loader *picked* as that fallback when
    the document declared none, so the analyzer can tell an authored ``default``
    from an inferred one. Injected rather than derived: only the loader knows
    which key it aliased, and by the time the map reaches here the alias is
    indistinguishable from a deliberate one.
    """

    def __init__(
        self,
        policies: dict[str, AgentPolicy],
        *,
        aliased_default: str | None = None,
    ) -> None:
        if DEFAULT_ROLE_NAME not in policies:
            raise PolicySetError(
                f"PolicySet missing required '{DEFAULT_ROLE_NAME}' role"
            )
        _validate_const_refs(policies)
        _validate_path_refs(policies, _RUN)
        self._agent_usage_paths = _validate_path_refs(policies, _AGENT_USAGE)
        self._policies = policies
        self._aliased_default = aliased_default

    @property
    def aliased_default(self) -> str | None:
        """The named role ``default`` silently falls back to, if any.

        ``None`` when the document declared its own ``default`` (or is a legacy
        flat file, which *is* the default role). Otherwise every role name the
        policy doesn't define resolves to this role's grants.
        """
        return self._aliased_default

    def policy_for(self, role: str | None) -> AgentPolicy:
        """Return the effective policy for ``role`` (or the default fallback)."""
        if role is None:
            return self._policies[DEFAULT_ROLE_NAME]
        return self._policies.get(role, self._policies[DEFAULT_ROLE_NAME])

    def evaluate(
        self,
        *,
        role: str | None,
        tool: str,
        args: Mapping[str, Any],
        attributes: Mapping[str, Any] | None = None,
        run: Mapping[str, Any] | None = None,
        agent_usage: Mapping[str, Any] | None = None,
    ) -> Verdict:
        """:class:`~hexgate.security.decision.PolicyEngine` entry point.

        Resolves the role's policy and runs the pydantic engine. ``attributes``
        feed the ``ctx.*`` constraint namespace, ``run`` the ``run.*`` one and
        ``agent_usage`` the ``agent_usage.*`` one; the role still selects the
        policy bucket (``policy_for``) on its own."""
        from hexgate.security.policy import evaluate_tool_call

        return evaluate_tool_call(
            self.policy_for(role),
            tool,
            dict(args),
            role=role,
            attributes=attributes,
            run=run,
            agent_usage=agent_usage,
        )

    @property
    def roles(self) -> list[str]:
        """List of role names, including ``default``, excluding mixins."""
        return sorted(self._policies)

    def declares_admission(self) -> bool:
        """True if any resolved role carries the ``agent.run`` key.

        Derived from ``effective_tools`` rather than a source field so it holds
        after inheritance and module folding, where the ``admission`` block has
        become an ``agent.run`` key (R-AGENT-002)."""
        return any(
            AGENT_RUN_TOOL in policy.effective_tools
            for policy in self._policies.values()
        )

    def declares_reach(self) -> bool:
        """True if any resolved role carries an ``agent.tool:`` / ``agent.handoff:`` key."""
        return any(
            any(is_agent_reach_key(key) for key in policy.effective_tools)
            for policy in self._policies.values()
        )

    def declares_tool_reach(self) -> bool:
        """True if any resolved role carries an ``agent.tool:`` (agent-as-tool) key."""
        return any(
            any(is_agent_via_key(key, "tool") for key in policy.effective_tools)
            for policy in self._policies.values()
        )

    def declares_skills(self) -> bool:
        """True if any resolved role carries a ``skill:`` / ``skill.*:`` key.

        Derived from ``effective_tools`` rather than ``skills`` so it holds after
        inheritance and module folding, where the block has become lowered keys."""
        return any(
            any(is_skill_key(key) for key in policy.effective_tools)
            for policy in self._policies.values()
        )

    def agent_usage_paths(self) -> frozenset[str]:
        """Every ``agent_usage.*`` path (without the root) any role references.

        Derived from ``effective_tools`` like the ``declares_*`` family, so
        admission and reach constraints count."""
        return self._agent_usage_paths

    def guard_stance(self) -> dict[str, dict] | None:
        """The agent-level guard enable/disable stance to carry in the bundle.

        Agent-level, not per-role (R-GUARD-007): the guard pipeline is built once at
        construction, before any caller role exists, so a single stance governs the
        agent. v1 is baseline-only (R-GUARD-006), and the baseline is uniform — it
        applies to every tool and every caller alike — so the only requirement is that
        every resolved role agree on it. A policy whose roles set *different* baselines
        cannot be represented, so this fails loud rather than silently pick one (which
        would enforce one role's disables on callers of another — a fail-open in the
        disable direction). A role silent on a baseline guard runs it (enabled default),
        so silence and an explicit ``enabled: false`` genuinely diverge; put shared
        baseline guards in a mixin every role inherits.

        Returns ``{"baseline": {name: enabled}}``, or ``None`` when no role configures
        any guard — so the bundle omits the section and a guards-free policy's manifest
        stays byte-identical (R-GUARD-006 / R-GUARD-007).

        Memoized: ``effective_guards`` / ``governed_guard_names`` call this per guarded
        call, and the stance is fixed after load, so it is computed once — including a
        :class:`PolicySetError`, which is cached and re-raised rather than recomputed,
        so a live divergent fallback engine does not re-run (and re-log) every call.
        """
        cached = getattr(self, "_guard_stance_cache", _UNSET)
        if cached is not _UNSET:
            if isinstance(cached, PolicySetError):
                raise cached
            return cached
        try:
            stance = self._compute_guard_stance()
        except PolicySetError as exc:
            self._guard_stance_cache: dict[str, dict] | PolicySetError | None = exc
            raise
        self._guard_stance_cache = stance
        return stance

    def _compute_guard_stance(self) -> dict[str, dict] | None:
        """Fold every role's baseline into one agent-level stance (see
        :meth:`guard_stance`); raise :class:`PolicySetError` on a divergent baseline."""
        baselines = {
            json.dumps({n: r.enabled for n, r in p.guards.items()}, sort_keys=True)
            for p in self._policies.values()
        }
        if len(baselines) > 1:
            roles = ", ".join(sorted(self._policies))
            raise PolicySetError(
                "baseline guards must resolve to the same stance across all roles (v1 "
                f"governs the agent, not the caller); roles {roles} differ. Put shared "
                "guards in a mixin every role inherits."
            )
        baseline = json.loads(next(iter(baselines))) if baselines else {}
        return {"baseline": baseline} if baseline else None

    def effective_guards(self, tool_name: str) -> dict[str, bool]:
        """Agent-level guard stance (R-GUARD-007). Baseline-only in v1, so uniform for
        every tool — ``tool_name`` is accepted for a signature shared with the bundle
        mirror but does not change the result.

        The readable-engine mirror of :meth:`PolicyBundle.effective_guards`, so the
        guarded runner reads the stance identically whether the engine is a compiled
        bundle or this set. Empty when no guard is configured."""
        stance = self.guard_stance()
        return dict(stance["baseline"]) if stance else {}

    def governed_guard_names(self) -> frozenset[str]:
        """Every guard name the stance references (R-GUARD-007), for the closed-world
        check. Mirror of :meth:`PolicyBundle.governed_guard_names`."""
        stance = self.guard_stance()
        return frozenset(stance["baseline"]) if stance else frozenset()

    def __contains__(self, role: str) -> bool:
        return role in self._policies

    def __repr__(self) -> str:
        return f"PolicySet(roles={self.roles!r})"


def _raw_constraints(
    policy: AgentPolicy, *, tools: Iterable[BaseToolPolicy]
) -> Iterator[str]:
    """Every constraint string a role evaluates, policy-level first.

    ``tools`` is a parameter because the two validators walk different views:
    const refs cover the lowered ``agent.*`` keys, run refs only authored ones.
    """
    yield from policy.constraints
    for tool_policy in (*tools, policy.default_policy):
        yield from tool_policy.constraints


def _validate_const_refs(policies: Mapping[str, AgentPolicy]) -> None:
    """Reject a ``consts.<name>`` reference to a constant not defined for its role.

    Runs at :class:`PolicySet` construction so the pydantic engine and the Rego
    compiler agree on whether a policy is valid — otherwise an undefined const
    loads cleanly and denies at runtime on pydantic, but fails the WASM build.
    Constraints are already grammar-validated at model load; this is the
    cross-reference check that needs the role's resolved ``consts``.
    """
    for role, policy in policies.items():
        available = set(policy.consts)
        for raw in _raw_constraints(policy, tools=policy.effective_tools.values()):
            for name in iter_const_refs(parse_constraint(raw)):
                if name not in available:
                    raise PolicySetError(
                        f"role {role!r}: constraint {raw!r} references "
                        f"undefined constant consts.{name}"
                    )


def _sdk_version() -> str:
    try:
        return version("hexgate")
    except PackageNotFoundError:  # pragma: no cover - editable installs always resolve
        return "unknown"


@dataclass(frozen=True, slots=True)
class _PathRoot:
    """A flat ``<root>.<name>`` namespace the linter closes."""

    name: str
    scalar_paths: frozenset[str]
    list_paths: frozenset[str]
    # What "this SDK knows" renders as in the unknown-path error.
    vocabulary: str

    @property
    def known(self) -> frozenset[str]:
        return self.scalar_paths | self.list_paths


def _run_root(scalar_paths: frozenset[str], list_paths: frozenset[str]) -> _PathRoot:
    return _PathRoot(
        "run",
        scalar_paths,
        list_paths,
        vocabulary=", ".join(sorted(scalar_paths | list_paths)),
    )


_RUN = _run_root(SCALAR_PATHS, LIST_PATHS)
_AGENT_USAGE = _PathRoot(
    "agent_usage",
    KNOWN_AGENT_USAGE_PATHS,
    frozenset(),
    vocabulary=AGENT_USAGE_VOCABULARY,
)


def _rooted_paths_in(node: Node, root: str) -> Iterator[tuple[str, ...]]:
    """Every ``root``-rooted path in a node, whatever its position."""
    for path in iter_arg_refs(node):
        if path and path[0] == root:
            yield path


def _validate_path_refs(
    policies: Mapping[str, AgentPolicy], root: _PathRoot
) -> frozenset[str]:
    """Reject a ``<root>.*`` reference this SDK cannot answer, or answers silently;
    return every ``<root>.*`` name referenced, without the root.

    Sibling of :func:`_validate_const_refs` — same construction-time check, same
    reason (pydantic and the Rego compiler must agree a policy is valid).

    Two failure modes, both otherwise silent: an unknown or too-deep path
    (``run.tool_call``, ``run.id.value``) resolves to missing and denies every
    call; a list-valued path used as a scalar (``run.tools_used not in [...]``)
    can silently *pass* every call instead.
    """
    referenced: set[str] = set()
    for role, policy in policies.items():
        # effective_tools, not tools — so a ref on a lowered agent key (admission
        # ``agent.run`` / reach ``agent.tool:``/``agent.handoff:``) is validated
        # too, matching :func:`_validate_const_refs`. Walking only ``tools``
        # fail-opened run.* constraints on those keys.
        for raw in _raw_constraints(policy, tools=policy.effective_tools.values()):
            node = parse_constraint(raw)
            _reject_unknown_paths(node, role, raw, root)
            _reject_list_paths_in_scalar_position(node, role, raw, root)
            referenced.update(path[1] for path in _rooted_paths_in(node, root.name))
    return frozenset(referenced)


def _reject_unknown_paths(node: Node, role: str, raw: str, root: _PathRoot) -> None:
    for path in _rooted_paths_in(node, root.name):
        if len(path) != _ROOTED_PATH_SEGMENTS:
            raise PolicySetError(
                f"role {role!r}: constraint {raw!r} references {root.name}.* path "
                f"{'.'.join(path[1:])!r}; {root.name}.* paths are exactly two "
                f"segments ({root.name}.<name>)"
            )
        if path[1] not in root.known:
            raise PolicySetError(
                f"role {role!r}: constraint {raw!r} references unknown "
                f"{root.name}.* path {path[1]!r} (hexgate {_sdk_version()} knows: "
                f"{root.vocabulary}). Upgrade the SDK or fix the path."
            )


def _reject_list_paths_in_scalar_position(
    node: Node, role: str, raw: str, root: _PathRoot
) -> None:
    for operand, op, side in iter_cmp_operands(node):
        # Ref-only: a Count is the correct way to use a list here.
        if not isinstance(operand, Ref) or op not in _SCALAR_ONLY_OPS:
            continue
        if len(operand.path) != _ROOTED_PATH_SEGMENTS or operand.path[0] != root.name:
            continue
        if operand.path[1] not in root.list_paths:
            continue
        # in/not in only accept a literal or const on the right, so a
        # list-valued ref can only ever be their left operand.
        if op in _ORDERED_OPS or side == LEFT:
            name = ".".join(operand.path)
            effect = (
                "silently passes" if op == "not in" else "silently fails every call"
            )
            raise PolicySetError(
                f"role {role!r}: constraint {raw!r} uses the list-valued path "
                f"{name!r} with {op!r}, which {effect}. Use: "
                f'not any({name}, . == "<value>")'
            )


def load_policy_set(source: str | Path | AgentPolicy | None) -> PolicySet:
    """Load a :class:`PolicySet` from disk, a single policy file, or an in-memory model.

    Three input shapes accepted:

    * ``Path`` pointing at a directory ending in ``policies`` — every
      ``*.yaml`` file inside is loaded as a role; the file stem is the role
      name. Inheritance is resolved and mixins are inlined.
    * ``Path`` pointing at a single YAML file (legacy ``policy.yaml``) —
      treated as the single ``default`` role; inheritance fields ignored.
    * An already-validated :class:`AgentPolicy` model — used as the
      ``default`` role; useful in tests.

    A ``PolicySet`` always carries a ``default`` role. If the directory
    doesn't ship a ``default.yaml``, the most-permissive non-mixin role
    becomes the fallback — operators should ship an explicit
    ``default.yaml`` to avoid this guesswork.
    """
    if source is None:
        return PolicySet({DEFAULT_ROLE_NAME: AgentPolicy()})
    if isinstance(source, AgentPolicy):
        return PolicySet({DEFAULT_ROLE_NAME: source})

    path = Path(source)
    if path.is_dir():
        return _load_from_directory(path)
    return _load_legacy_file(path)


def load_policy_map(
    policy_map: dict[str, AgentPolicy], default: str | None = None
) -> PolicySet:
    """Build a :class:`PolicySet` from a plain ``{role: AgentPolicy}`` dict.

    Used by the cloud loader and by inline-roles ``policy.yaml`` files. Each
    role's ``inherits`` field is resolved against the rest of the map before
    mixin filtering — mirrors the ``_load_from_directory`` path so both
    storage shapes produce the same effective policies.

    ``default`` names the role to use as the fallback. Defaults to
    ``"default"`` if the dict contains it, otherwise the alphabetically first
    concrete role — matching what a ``policies/`` directory infers.
    """
    if not policy_map:
        raise PolicySetError("policy_map must contain at least one role")
    # Resolve inheritance against the full map (including mixins, which can
    # only be referenced via ``inherits``).
    fully_resolved: dict[str, AgentPolicy] = {}
    for role_name in policy_map:
        fully_resolved[role_name] = _resolve_inheritance(
            role_name, policy_map, chain=[]
        )
    resolved = {name: pol for name, pol in fully_resolved.items() if not pol.is_mixin}
    if not resolved:
        raise PolicySetError("policy_map contains only mixins; need a concrete role")
    # An explicit ``default=`` is the caller's deliberate choice; only the
    # fallback we *infer* here is worth warning about downstream.
    inferred = default is None and DEFAULT_ROLE_NAME not in resolved
    # ``sorted()``, not insertion order: ``_load_from_directory`` infers its
    # fallback alphabetically, and the same role set must resolve undefined role
    # names to the same policy however the document was loaded.
    default_name = default or (
        DEFAULT_ROLE_NAME if DEFAULT_ROLE_NAME in resolved else sorted(resolved)[0]
    )
    if default_name not in resolved:
        raise PolicySetError(
            f"requested default role {default_name!r} not in {sorted(resolved)!r}"
        )
    if default_name != DEFAULT_ROLE_NAME:
        resolved[DEFAULT_ROLE_NAME] = resolved[default_name]
    return PolicySet(resolved, aliased_default=default_name if inferred else None)


def _load_legacy_file(path: Path) -> PolicySet:
    """Load a single ``policy.yaml`` — flat or inline-roles shape."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return load_policy_set_from_dict(payload)


def _reject_unknown_file_level_keys(payload: dict[str, Any]) -> None:
    """Fail closed on an unrecognised sibling of ``roles:``."""
    unknown = sorted(set(payload) - _FILE_LEVEL_KEYS)
    if unknown:
        allowed = sorted(_FILE_LEVEL_KEYS - {_ROLES_KEY, RESOLVED_POLICY_MARKER})
        raise PolicySetError(
            f"unrecognised top-level key(s) {unknown} beside {_ROLES_KEY!r}; "
            f"a role-keyed document reads policy fields inside each role, so "
            f"move them under a role. Only {allowed} are file-level keys."
        )


def _file_level_constraints(payload: dict[str, Any]) -> list[str]:
    """The file-level fence list, or empty. Shape is checked before the hoist:
    a bare string would splat into characters, each a valid ``str``."""
    raw = payload.get(_CONSTRAINTS_KEY)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise PolicySetError(
            f"top-level {_CONSTRAINTS_KEY!r} must be a list of constraint "
            f"expressions (got {type(raw).__name__})"
        )
    return raw


def _with_file_level_constraints(spec: Any, fences: list[str]) -> Any:
    """Union the file-level fences ahead of a role's own — replacing would let a
    role drop the file's fence, which is fail-open. Non-mappings pass through."""
    if not fences or not isinstance(spec, dict):
        return spec
    own = spec.get(_CONSTRAINTS_KEY, [])
    if not isinstance(own, list):
        return spec
    return {**spec, _CONSTRAINTS_KEY: [*fences, *own]}


def load_policy_set_from_dict(payload: dict[str, Any]) -> PolicySet:
    """Build a :class:`PolicySet` from an already-parsed YAML document.

    Detects which shape the document carries and dispatches accordingly:

    * **Inline roles shape** — has a top-level ``roles:`` key whose value is
      a mapping of ``{role_name: agent_policy_spec}``. Each value is validated
      as an :class:`AgentPolicy`; the resulting map is wrapped via
      :func:`load_policy_map` so inheritance and mixin filtering apply.

      A file-level ``constraints:`` block unions into every role, so the key
      means the same thing in both shapes. ``version`` is file metadata; any
      other sibling of ``roles:`` is rejected rather than silently dropped.

    * **Flat single-policy shape** (legacy) — anything else is treated as a
      single :class:`AgentPolicy` and wrapped as the ``default`` role.

    A top-level ``_resolved: true`` marker (set by the resolve serializer,
    :data:`RESOLVED_POLICY_MARKER`) validates under a ``{"resolved": True}``
    context: a machine-resolved policy legitimately carries lowered ``agent.*``
    keys in ``tools`` and must round-trip back through this loader when a modular
    agent's bundle is compiled from its resolved YAML (R-POL-002). A hand-written
    policy has no marker, so the reserved-``agent.*``-name guard still fires on it.

    Used by the cloud loader (the platform returns one ``policy_yaml`` string
    per agent, with roles potentially inline) and by ``_load_legacy_file``
    for SDK-local agents.
    """
    context = {"resolved": True} if payload.get(RESOLVED_POLICY_MARKER) else None
    if isinstance(payload.get(_ROLES_KEY), dict):
        _reject_unknown_file_level_keys(payload)
        fences = _file_level_constraints(payload)
        role_policies = {
            role_name: AgentPolicy.model_validate(
                _with_file_level_constraints(spec or {}, fences), context=context
            )
            for role_name, spec in payload[_ROLES_KEY].items()
        }
        return load_policy_map(role_policies)
    flat = {k: v for k, v in payload.items() if k != RESOLVED_POLICY_MARKER}
    return PolicySet(
        {DEFAULT_ROLE_NAME: AgentPolicy.model_validate(flat, context=context)}
    )


def _load_from_directory(root: Path) -> PolicySet:
    """Walk ``root/*.yaml``, parse each as an :class:`AgentPolicy`, resolve inheritance."""
    raw: dict[str, AgentPolicy] = {}
    for file in sorted(root.glob("*.yaml")):
        name = file.stem
        payload = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        try:
            raw[name] = AgentPolicy.model_validate(payload)
        except Exception as exc:
            raise PolicySetError(f"policy {file.name!r} is invalid: {exc}") from exc
    if not raw:
        raise PolicySetError(f"no policy files found in {root}")

    resolved: dict[str, AgentPolicy] = {}
    for name in raw:
        resolved[name] = _resolve_inheritance(name, raw, chain=[])

    concrete = {name: pol for name, pol in resolved.items() if not pol.is_mixin}
    if not concrete:
        raise PolicySetError(
            f"every policy in {root} is a mixin; need at least one concrete role"
        )

    aliased: str | None = None
    if DEFAULT_ROLE_NAME not in concrete:
        # No explicit default — pick the first concrete role alphabetically and
        # alias it as the fallback. Loud but not fatal; operators should drop
        # in an explicit default.yaml, which the ``implicit-default`` lint says.
        aliased = sorted(concrete)[0]
        concrete[DEFAULT_ROLE_NAME] = concrete[aliased]
    return PolicySet(concrete, aliased_default=aliased)


def _resolve_inheritance(
    name: str, raw: dict[str, AgentPolicy], chain: list[str]
) -> AgentPolicy:
    """Recursively flatten ``inherits`` for one role.

    ``inherits: [A, B]`` means the parents are merged in declaration order,
    then this role's own fields overlay last. Equivalent to Python's MRO
    with explicit precedence: ``self`` wins, then later parents, then
    earlier parents.

    ``constraints`` is the one exception: it **unions** across the chain, since
    letting a child replace a mixin's cap would silently remove it. Union can
    only narrow. Deduplicated in first-seen order, so a diamond does not
    evaluate the same predicate twice.
    """
    if name in chain:
        raise PolicySetError(f"cyclic inheritance: {' -> '.join(chain + [name])}")
    if name not in raw:
        raise PolicySetError(
            f"role {name!r} not found (inherited from {chain[-1] if chain else '<root>'})"
        )
    own = raw[name]
    if not own.inherits:
        return own
    merged_tools: dict[str, ToolPolicy] = {}
    merged_agents: dict[str, AgentTargetPolicy] = {}
    merged_skills: dict[str, SkillPolicy] = {}
    merged_guards: dict[str, GuardRule] = {}
    merged_consts: dict[str, object] = {}
    merged_constraints: list[str] = []
    merged_default: BaseToolPolicy = own.default_policy
    merged_admission: BaseToolPolicy | None = own.admission

    # Merge parents left-to-right (later parents override earlier). ``agents`` and
    # ``skills`` merge by name like ``tools`` does. ``admission`` only overwrites when
    # a parent actually sets one, so a later mixin that omits it can't null out an
    # earlier parent's rule — dropping an agent gate silently would be fail-open.
    for parent_name in own.inherits:
        parent = _resolve_inheritance(parent_name, raw, chain + [name])
        merged_tools.update(parent.tools)
        merged_agents.update(parent.agents)
        merged_skills.update(parent.skills)
        merged_guards.update(parent.guards)
        merged_consts.update(parent.consts)
        _extend_unique(merged_constraints, parent.constraints)
        merged_default = parent.default_policy
        if parent.admission is not None:
            merged_admission = parent.admission

    # Self overrides everything from parents. Check ``model_fields_set`` rather
    # than comparing against ``BaseToolPolicy()``: a child that explicitly says
    # ``default_policy: { mode: deny }`` is value-equal to the default but the
    # user's intent is to override, and silently inheriting an ``allow`` from a
    # parent would be fail-open.
    merged_tools.update(own.tools)
    merged_agents.update(own.agents)
    merged_skills.update(own.skills)
    merged_guards.update(own.guards)
    merged_consts.update(own.consts)
    # Union, not override — the one field here that accumulates (see docstring).
    _extend_unique(merged_constraints, own.constraints)
    if "default_policy" in own.model_fields_set:
        merged_default = own.default_policy
    if "admission" in own.model_fields_set:
        merged_admission = own.admission

    return AgentPolicy(
        version=own.version,
        inherits=own.inherits,
        is_mixin=own.is_mixin,
        default_policy=merged_default,
        constraints=merged_constraints,
        tools=merged_tools,
        consts=merged_consts,
        admission=merged_admission,
        agents=merged_agents,
        skills=merged_skills,
        guards=merged_guards,
    )


def _extend_unique(target: list[str], values: Iterable[str]) -> None:
    """Append values not already present, preserving first-seen order."""
    for value in values:
        if value not in target:
            target.append(value)
