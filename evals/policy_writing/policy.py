"""Load, validate and dry-run a policy the way `hexgate policy` does, via the SDK.

`effective_policy` runs what `validate` (single file) or `check` + `resolve`
(module tree) runs, failing on lint warnings but the drift ones, which it
keeps for the name checks (`DRIFT_CODES`); `decide` runs what `test` runs,
with the CLI's input checks. On an opt-in gate the policy never declares, it
follows the runtime where `test` would deny: an admission or handoff call is
allowed, and an agent-as-tool or skill call is refused as a case error, since
the runtime decides it under the tool's own name. A call on a declared gate is
dry-run with the args that gate sends at runtime: the case gives only those
the gate doesn't set itself (a skill read's `file_path` and `content_hash`),
or ones it sets with the value it sets them to; any other is refused.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import get_args

import yaml
from pydantic import TypeAdapter, ValidationError

from evals.policy_writing.sources import ProjectAgents, SourceError, load_project_agents
from hexgate.manifest.models import AgentManifest
from hexgate.runtime.context import ContextAttributeValue
from hexgate.runtime.run_facts import DETACHED, KNOWN_RUN_PATHS
from hexgate.security import (
    RESOLVED_POLICY_MARKER,
    DecisionOutcome,
    LinkError,
    ModuleContent,
    PolicySet,
    PolicySetError,
    RoleMatrix,
    check_project,
    compile_to_rego,
    effective_policy_by_role,
    load_local_modules,
    load_policy_set_from_dict,
    load_roles,
    resolve_for_project,
)
from hexgate.security.analyzer import (
    SEVERITY_RANK,
    LintCode,
    PolicyLint,
    analyze_policy,
)
from hexgate.security.constraints import ConstraintParseError
from hexgate.security.decision import Verdict
from hexgate.security.models import (
    AGENT_RUN_TOOL,
    AgentVia,
    PolicyMode,
    SkillVia,
    agent_target_key,
    gate_args,
    skill_key,
)
from hexgate.security.modules import DEFAULT_AGENT
from hexgate.security.naming import (
    DEFAULT_AGENT_NAME,
    canonical_name,
    canonical_skill_name,
)
from hexgate.security.network import EGRESS_TOOL_ARGS
from hexgate.security.testing import run_namespace

# Everything the SDK raises for a policy it can't load, compile or link.
POLICY_ERRORS = (PolicySetError, ConstraintParseError, LinkError, ValidationError)
# The lints a manifest turns on for a name the agent's code doesn't declare
# (`analyze_policy`, `check_project`). The name checks report them, at any
# severity, so an invented name fails one check rather than `valid`.
DRIFT_CODES: frozenset[LintCode] = frozenset(
    {"unknown-tool", "unknown-skill", "unknown-guard", "unknown-arg"}
)


@dataclass
class Policy:
    """The policy the checks run against.

    `payload` is the document: `policy.yaml` as written, or a module tree's
    resolved roles. `policy_set` is it loaded, ready to evaluate. `agent` is
    the agent running the calls: an agent gate sends it as `args.agent`, and
    it is `run.agent` once the run has started. `manifest` is that agent's from
    agents.json, if it has a readable one, and `drift` the SDK's lints in
    `DRIFT_CODES`: against that manifest for a policy file, against every
    agent's for a module tree.
    """

    payload: dict
    policy_set: PolicySet
    agent: str | None = None
    manifest: AgentManifest | None = None
    drift: list[PolicyLint] = field(default_factory=list)


# A case file names outcomes by their policy mode, not by the enum's values.
LABELS: dict[DecisionOutcome, PolicyMode] = {
    DecisionOutcome.ALLOW: "allow",
    DecisionOutcome.DENY: "deny",
    DecisionOutcome.NEEDS_APPROVAL: "approval_required",
}
# How freely each outcome lets a call through, for comparing two roles.
RANK = {
    DecisionOutcome.DENY: 0,
    DecisionOutcome.NEEDS_APPROVAL: 1,
    DecisionOutcome.ALLOW: 2,
}


class CaseError(ValueError):
    """A case's call can't be dry-run: an undefined role, bad attributes or run facts,
    or args a declared gate sets itself to other values."""


_ATTRIBUTES = TypeAdapter(dict[str, ContextAttributeValue])


_BY_LABEL = {label: value for value, label in LABELS.items()}


def outcome(label: str) -> DecisionOutcome:
    """The outcome a case file names (`allow`, `deny`, `approval_required`)."""
    return _BY_LABEL[label]


@dataclass(frozen=True)
class _Gate:
    """A call on a gate: the key it's decided under, the args the gate sets
    itself, whether the policy declares the gate, and where the runtime
    decides the call.

    Undeclared, admission and handoff check nothing and the call goes through
    (agent_gate.py, the runners' handoff seam). Agent-as-tool and skill calls
    are decided under the tool's own name instead (guards/runner.py,
    `policy_key or call.tool_name`), so the key can't predict them: `by_name`.
    A top-level agent's admission is decided before its run starts
    (`_check_admission`, then `run_scope`, in agents/factory.py and the
    runners): not `in_run`.
    """

    key: str
    sent: dict
    name: str
    declared: bool
    by_name: bool
    in_run: bool = True


# Per reach mode: the gate's name, whether a policy declares it, and `by_name`.
_REACH: dict[str, tuple[str, Callable[[PolicySet], bool], bool]] = {
    "tool": ("agent-as-tool reach", PolicySet.declares_tool_reach, True),
    "handoff": ("reach", PolicySet.declares_reach, False),
}


def _gate(tool: str, agent: str, policy_set: PolicySet) -> _Gate | None:
    """The gate `tool` is on, or None for a plain tool call.

    Admission sets `{agent}`, reach `{agent, target, via}` with the target
    trimmed as the key is (`AgentGate._decide`, `ReachGate._decide`), and a
    skill gate `{skill, via}` with the name trimmed as `skill_key` does.
    """
    if tool == AGENT_RUN_TOOL:
        declared = policy_set.declares_admission()
        return _Gate(tool, {"agent": agent}, "admission", declared, False, in_run=False)
    for via in get_args(AgentVia):
        prefix = agent_target_key(via, "")
        if tool.startswith(prefix):
            name, declares, by_name = _REACH[via]
            target = canonical_name(tool.removeprefix(prefix))
            sent = {"agent": agent, "target": target, "via": via}
            key = agent_target_key(via, target)
            return _Gate(key, sent, name, declares(policy_set), by_name)
    for via in get_args(SkillVia):
        prefix = skill_key(via, "")
        if tool.startswith(prefix):
            name = canonical_skill_name(tool.removeprefix(prefix))
            sent = {"skill": name, "via": via}
            declared = policy_set.declares_skills()
            return _Gate(skill_key(via, name), sent, "skills", declared, by_name=True)
    return None


def _with_gate_args(gate: _Gate, args: dict) -> dict:
    """`args` as the gate passes them: what it sets itself, plus the rest of
    `gate_args` from the case (a skill read's `file_path`, the skill's
    `content_hash`, which the adapter hashes from the skill), None where the
    case leaves one out, as the adapters send it.

    A case may spell an arg the gate sets only with the gate's value, as the
    case loader (PR 2) fills it in; any other value would be silently replaced.
    """
    from_call = gate_args(gate.key) - gate.sent.keys()
    if extra := sorted(
        k
        for k, v in args.items()
        if k not in from_call and (k not in gate.sent or v != gate.sent[k])
    ):
        raise CaseError(
            f"{gate.key}: the gate sends {gate.sent} itself, and the case "
            f"gives only {sorted(from_call)}; drop {extra}"
        )
    return {**dict.fromkeys(from_call), **args, **gate.sent}


def dump_json(value: object) -> str:
    """`value` as JSON; an unquoted YAML date, kept as a date by the case
    loader, becomes its string."""
    return json.dumps(value, sort_keys=True, default=str)


def _as_json(value: dict) -> dict:
    """`value` as `policy test --args` / `--attributes` receive it, through JSON,
    so a date compares with `>= "2026-01-01"` as text."""
    return json.loads(dump_json(value))


def _run(key: str, gate: _Gate | None, agent: str, facts: dict) -> dict:
    """`run.*` as the runtime has it when it decides `key`: none outside a
    run, else the agent's run, zeroed as `policy test --run-facts` builds it."""
    # Egress is decided by a proxy above every run (docs/policy/constraints.mdx,
    # "Where facts aren't collected").
    if (gate and not gate.in_run) or key in EGRESS_TOOL_ARGS:
        if facts:
            raise CaseError(f"{key} is decided outside any run; drop run_facts")
        return DETACHED.as_namespace(key)
    # Checked here: a `tool` key would collide with `run_namespace`'s parameter.
    if unknown := sorted(facts.keys() - KNOWN_RUN_PATHS):
        raise CaseError(f"unknown run.* path(s) {unknown}")
    if "agent" in facts:
        raise CaseError("run.agent is the case's agent; drop run_facts.agent")
    # The runtime counts a call under the tool's own name, never a gate key
    # (guards/runner.py, `_record_run_execution(call.tool_name)`).
    if gate and "calls_of_this_tool" in facts:
        raise CaseError(f"{key} is never counted; drop run_facts.calls_of_this_tool")
    return run_namespace("" if gate else key, agent=agent, **facts)


def check_run_facts(d: dict, agent: str) -> None:
    """Raise `CaseError` for run facts `decide` refuses on `d` under any policy,
    so the case loader rejects them before a run: whether a policy declares
    the call's gate never changes the run `decide` builds."""
    gate = _gate(d["tool"], agent, load_policy_set_from_dict({}))
    try:
        _run(gate.key if gate else d["tool"], gate, agent, _as_json(d["run_facts"]))
    except ValueError as exc:  # CaseError is one
        raise CaseError(str(exc)) from exc


def decide(policy: Policy, role: str, d: dict) -> Verdict:
    """Dry-run one call, with the same inputs as `hexgate policy test`, except that
    a call on a gate carries the args that gate sends at runtime.

    Raises `CaseError` where the CLI would refuse the call, or where the eval
    can't judge it (args a declared gate sets itself to other values). An
    undefined role is one, rather than the `default` fallback: a case naming a
    role the policy lacks fails instead of passing by luck.
    """
    if role not in policy.policy_set:
        raise CaseError(f"role {role!r} not in policy ({policy.policy_set.roles})")
    # The name the runtime gives the agent, untrimmed.
    agent = policy.agent or DEFAULT_AGENT_NAME
    gate = _gate(d["tool"], agent, policy.policy_set)
    key = gate.key if gate else d["tool"]
    try:  # a pydantic ValidationError is a ValueError
        args = _as_json(d.get("args", {}))
        attributes = _ATTRIBUTES.validate_python(_as_json(d.get("attributes", {})))
        run = _run(key, gate, agent, _as_json(d.get("run_facts", {})))
    except ValueError as exc:
        raise CaseError(str(exc)) from exc
    if gate and not gate.declared:
        if gate.by_name:
            raise CaseError(
                f"{key}: {gate.name} isn't declared, so the runtime decides this "
                "call under the tool's own name; dry-run that tool instead"
            )
        return Verdict(DecisionOutcome.ALLOW, reason="gate not declared")
    if gate:
        args = _with_gate_args(gate, args)
    return policy.policy_set.evaluate(
        role=role, tool=key, args=args, attributes=attributes, run=run
    )


def format_lint(lint: PolicyLint) -> str:
    """`lint` tagged with its cell, as `policy check` tags it: an
    `unknown-agent` message doesn't name its column."""
    return (
        f"[{lint.code}]"
        + (f" [{lint.role}]" if lint.role else "")
        + (f" [agent {lint.agent}]" if lint.agent else "")
        + f" {lint.message}"
    )


def _split(lints: list[PolicyLint]) -> tuple[list[PolicyLint], list[str]]:
    """`lints` as (drift for the name checks, `valid`'s failures)."""
    # A warning fails, not only an error: the write-policy skill tells the agent
    # to validate with `--max-severity warning` (for `policy check` on a module
    # tree it doesn't yet; the spec aligns the skill in PR 11).
    drift = [lint for lint in lints if lint.code in DRIFT_CODES]
    failures = [
        format_lint(lint)
        for lint in lints
        if lint.code not in DRIFT_CODES
        and SEVERITY_RANK[lint.severity] <= SEVERITY_RANK["warning"]
    ]
    return drift, failures


def _load(
    payload: dict, manifest: AgentManifest | None
) -> tuple[PolicySet | None, list[PolicyLint], list[str]]:
    """What `hexgate policy validate` runs: load, compile, then `analyze_policy`
    with the agent's manifest (R-POL-003). Returns (policy set, drift, failures)."""
    try:
        policy_set = load_policy_set_from_dict(payload)
        # Compile too, so a policy the build would reject doesn't pass.
        compile_to_rego(payload)
    except POLICY_ERRORS as exc:
        return None, [], [str(exc)]
    except TypeError as exc:  # e.g. an unquoted YAML date the compiler can't serialise
        return None, [], [f"can't compile: {exc}"]
    return policy_set, *_split(analyze_policy(policy_set, manifest=manifest))


def _module_payload(
    ws: Path, agent: str, agents: ProjectAgents
) -> tuple[dict | None, list[PolicyLint], list[str]]:
    """What `hexgate policy check` and `resolve` do on a module tree, with the
    project's `agents`. Returns (resolved payload, drift, failures)."""
    try:
        boundaries, capabilities = load_local_modules(ws)
        roles = load_roles(ws)
    except (ValueError, OSError) as exc:
        return None, [], [str(exc)]
    if not boundaries and not capabilities:
        return (
            None,
            [],
            ["no modules under policies/boundaries/ or policies/capabilities/"],
        )
    # Lints the modules and every column's roles, as `policy check` does, with
    # the project's agents, so a roles column or `agents:` target naming no agent
    # fails; `_load` then lints the case agent's resolved roles as `validate` would.
    lints = check_project(
        boundaries,
        capabilities,
        roles,
        manifests=agents.manifests,
        registered_agents=agents.registered,
    )
    _, problems = _split(lints)
    if problems:
        return None, [], problems
    drift = _module_drift(boundaries, capabilities, roles, agents)
    try:
        # `agent`'s column of roles.yaml, as the platform builds that agent's bundle.
        result = resolve_for_project(boundaries, capabilities, roles, agent=agent)
    except POLICY_ERRORS as exc:
        return None, [], [str(exc)]
    payload = {"roles": effective_policy_by_role(result), RESOLVED_POLICY_MARKER: True}
    return payload, drift, []


def _module_drift(
    boundaries: list[ModuleContent],
    capabilities: list[ModuleContent],
    roles: RoleMatrix | None,
    agents: ProjectAgents,
) -> list[PolicyLint]:
    """The drift lints against the manifests in agents.json. An agent with no
    manifest, or a sub-agent with none of its own, lists no tools, so the roster
    is the agents with one: with the whole project's, `check_project` leaves
    `"*"` cells and boundaries unchecked, as a name may be the unknown agent's."""
    shown = {
        name: manifest.model_copy(update={"subagents": None})
        for name, manifest in agents.manifests.items()
    }
    if not shown:
        return []
    lints = check_project(boundaries, capabilities, roles, manifests=shown)
    return [lint for lint in lints if lint.code in DRIFT_CODES]


def _yaml_payload(ws: Path) -> tuple[dict | None, list[PolicyLint], list[str]]:
    """`policy.yaml` as `hexgate policy validate` reads it: an empty file is `{}`.
    Returns (payload, drift, failures), as `_module_payload` does; the drift
    comes from `_load`."""
    try:
        text = (ws / "policy.yaml").read_text(encoding="utf-8")
        payload = yaml.safe_load(text) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, [], [str(exc)]
    if not isinstance(payload, dict):
        return None, [], ["policy.yaml is not a YAML mapping"]
    return payload, [], []


def effective_policy(
    ws: Path, agent: str | None = None, modules: bool = False
) -> tuple[Policy | None, list[str]]:
    """The policy in `ws`, or why it doesn't validate: `hexgate policy validate`
    on `policy.yaml`, or with `modules`, `check` + `resolve` on the module tree
    for `agent`'s roles.yaml column."""
    try:
        agents = load_project_agents(ws)
    except SourceError as exc:
        agents, unreadable = None, f"agents.json unreadable: {exc}"[:300]
    manifest = agents.manifests.get(agent) if agents and agent else None
    if modules:
        if agents is None:
            return None, [unreadable]
        # The drift comes from `check_project`; the resolved roles are linted
        # manifest-free, as their `"*"` grants may name any agent's tools.
        payload, drift, problems = _module_payload(ws, agent or DEFAULT_AGENT, agents)
    else:
        # Without the case agent's manifest the drift lints don't run, and the
        # name checks fail on its absence.
        payload, drift, problems = _yaml_payload(ws)
    if payload is None:
        return None, problems
    policy_set, resolved_drift, problems = _load(payload, None if modules else manifest)
    if problems:
        return None, problems
    return Policy(payload, policy_set, agent, manifest, drift + resolved_drift), []
