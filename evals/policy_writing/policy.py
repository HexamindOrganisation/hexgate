"""Load, validate and dry-run a policy the way `hexgate policy` does, via the SDK.

`effective_policy` runs what `validate` (single file) or `check` + `resolve`
(module tree) runs, failing on lint warnings; `decide` runs what `test` runs,
with the CLI's input checks. On an opt-in gate the policy never declares, it
follows the runtime where `test` would deny: an admission or handoff call is
allowed, and an agent-as-tool or skill call is refused as a case error, since
the runtime decides it under the tool's own name. A call on a declared gate is
dry-run with the args that gate sends at runtime, so a case giving its own is
refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError

from hexgate.runtime.context import ContextAttributeValue
from hexgate.security import (
    RESOLVED_POLICY_MARKER,
    DecisionOutcome,
    LinkError,
    PolicySet,
    PolicySetError,
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
    PolicyLint,
    analyze_policy,
)
from hexgate.security.constraints import ConstraintParseError
from hexgate.security.decision import Verdict
from hexgate.security.models import (
    AGENT_RUN_TOOL,
    PolicyMode,
    agent_target_key,
    is_agent_reach_key,
    is_agent_via_key,
    is_skill_key,
)
from hexgate.security.modules import DEFAULT_AGENT
from hexgate.security.naming import DEFAULT_AGENT_NAME, canonical_name
from hexgate.security.testing import run_namespace

# Everything the SDK raises for a policy it can't load, compile or link.
POLICY_ERRORS = (PolicySetError, ConstraintParseError, LinkError, ValidationError)


@dataclass
class Policy:
    """The policy the checks run against.

    `payload` is the document: `policy.yaml` as written, or a module tree's
    resolved roles. `policy_set` is it loaded, ready to evaluate. `agent` is the
    agent running the calls, which an agent gate sends as `args.agent`.
    """

    payload: dict
    policy_set: PolicySet
    agent: str | None = None


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
    or args on a declared agent-gate call."""


_ATTRIBUTES = TypeAdapter(dict[str, ContextAttributeValue])


_BY_LABEL = {label: value for value, label in LABELS.items()}


def outcome(label: str) -> DecisionOutcome:
    """The outcome a case file names (`allow`, `deny`, `approval_required`)."""
    return _BY_LABEL[label]


def _undeclared_by_name(policy_set: PolicySet, tool: str) -> str | None:
    """The gate `tool` belongs to, if the runtime would decide it by another name.

    An undeclared agent-as-tool or skill gate isn't checked under its key: the
    call is decided under the tool's own name (guards/runner.py, `policy_key or
    call.tool_name`), so this key can't predict it.
    """
    if is_agent_via_key(tool, "tool") and not policy_set.declares_tool_reach():
        return "agent-as-tool reach"
    if is_skill_key(tool) and not policy_set.declares_skills():
        return "skills"
    return None


def _passes_unchecked(policy_set: PolicySet, tool: str) -> bool:
    """Whether `tool` is on an admission or handoff gate the policy never declares.

    Those gates check nothing then, and the call goes through (agent_gate.py, the
    runners' handoff seam); evaluating the key would deny it instead.
    """
    if tool == AGENT_RUN_TOOL:
        return not policy_set.declares_admission()
    return is_agent_via_key(tool, "handoff") and not policy_set.declares_reach()


def _as_json(value: dict) -> dict:
    """`value` as `policy test --args` / `--attributes` receive it, through JSON:
    an unquoted YAML date becomes its string, so `>= "2026-01-01"` compares alike."""
    return json.loads(json.dumps(value, default=str))


def _gate_call(tool: str, agent: str | None) -> tuple[str, dict | None]:
    """The key an agent gate decides under, and the args it sends; `(tool, None)`
    for any other call.

    The gates ignore a call's own args: admission sends `{agent}` and reach sends
    `{agent, target, via}`, with the target trimmed as the key is
    (`AgentGate._decide`, `ReachGate._decide`). The agent name is sent as the
    runtime sets it, `name or "default"`, untrimmed.
    """
    name = agent or DEFAULT_AGENT_NAME
    if tool == AGENT_RUN_TOOL:
        return tool, {"agent": name}
    if is_agent_reach_key(tool):
        kind, _, raw = tool.partition(":")
        via, target = kind.removeprefix("agent."), canonical_name(raw)
        gate_args = {"agent": name, "target": target, "via": via}
        return agent_target_key(via, target), gate_args
    return tool, None


def decide(policy: Policy, role: str, d: dict) -> Verdict:
    """Dry-run one call, with the same inputs as `hexgate policy test`, except that
    a call on an agent gate carries the args that gate sends at runtime.

    Raises `CaseError` where the CLI would refuse the call, or where the eval
    can't judge it (args on a declared agent-gate call). An undefined role is
    one, rather than the `default` fallback: a case naming a role the policy
    lacks fails instead of passing by luck.
    """
    if role not in policy.policy_set:
        raise CaseError(f"role {role!r} not in policy ({policy.policy_set.roles})")
    key, gate_args = _gate_call(d["tool"], policy.agent)
    try:
        args = _as_json(d.get("args", {}))
        attributes = _ATTRIBUTES.validate_python(_as_json(d.get("attributes", {})))
        # Over a zeroed run, so an unset `run.*` path reads 0, not missing; keyed
        # by the call's real key, so `run.tools_used` names what is decided.
        run = run_namespace(key, **_as_json(d.get("run_facts", {})))
    except (ValidationError, ValueError) as exc:
        raise CaseError(str(exc)) from exc
    if gate := _undeclared_by_name(policy.policy_set, key):
        raise CaseError(
            f"{key}: {gate} isn't declared, so the runtime decides this call "
            "under the tool's own name; dry-run that tool instead"
        )
    if _passes_unchecked(policy.policy_set, key):
        return Verdict(DecisionOutcome.ALLOW, reason="gate not declared")
    if gate_args is not None and args:  # inputs the dry-run couldn't use
        raise CaseError(f"{key}: the gate sends its own args; drop {sorted(args)}")
    if gate_args is not None:
        args = gate_args
    return policy.policy_set.evaluate(
        role=role,
        tool=key,
        args=args,
        attributes=attributes,
        run=run,
    )


def _lint_failures(lints: list[PolicyLint]) -> list[str]:
    # A warning fails, not only an error: the write-policy skill tells the agent
    # to validate with `--max-severity warning` (for `policy check` on a module
    # tree it doesn't yet; the spec aligns the skill in PR 11).
    return [
        f"[{lint.code}] {lint.message}"
        for lint in lints
        if SEVERITY_RANK[lint.severity] <= SEVERITY_RANK["warning"]
    ]


def _load(payload: dict) -> tuple[PolicySet | None, list[str]]:
    """What `hexgate policy validate` runs: load, compile, then `analyze_policy` (R-POL-003)."""
    try:
        policy_set = load_policy_set_from_dict(payload)
    except POLICY_ERRORS as exc:
        return None, [str(exc)]
    try:
        # Compile too, so a policy the build would reject doesn't pass.
        compile_to_rego(payload)
    except POLICY_ERRORS as exc:
        return None, [str(exc)]
    except TypeError as exc:  # e.g. an unquoted YAML date the compiler can't serialise
        return None, [f"can't compile: {exc}"]
    return policy_set, _lint_failures(analyze_policy(policy_set))


def _module_payload(ws: Path, agent: str) -> tuple[dict | None, list[str]]:
    """What `hexgate policy check` and `resolve` do on a module tree."""
    try:
        boundaries, capabilities = load_local_modules(ws)
        roles = load_roles(ws)
    except (ValueError, OSError) as exc:
        return None, [str(exc)]
    if not boundaries and not capabilities:
        return None, ["no modules under policies/boundaries/ or policies/capabilities/"]
    # Lints the modules (dead or erased grants), not the roles they compose
    # into: `_load` lints those on the resolved result.
    problems = _lint_failures(check_project(boundaries, capabilities, roles))
    if problems:
        return None, problems
    try:
        # `agent`'s column of roles.yaml, as the platform builds that agent's bundle.
        result = resolve_for_project(boundaries, capabilities, roles, agent=agent)
    except POLICY_ERRORS as exc:
        return None, [str(exc)]
    return {"roles": effective_policy_by_role(result), RESOLVED_POLICY_MARKER: True}, []


def _yaml_payload(ws: Path) -> tuple[dict | None, list[str]]:
    """`policy.yaml` as `hexgate policy validate` reads it: an empty file is `{}`."""
    try:
        payload = yaml.safe_load((ws / "policy.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        return None, [str(exc)]
    if not isinstance(payload, dict):
        return None, ["policy.yaml is not a YAML mapping"]
    return payload, []


def is_module_tree(ws: Path) -> bool:
    """A module tree (`policies/`) rather than a single `policy.yaml`."""
    return (ws / "policies").is_dir()


def effective_policy(
    ws: Path, agent: str | None = None
) -> tuple[Policy | None, list[str]]:
    """The policy in `ws`, or why it doesn't validate (`hexgate policy validate`,
    or `check` + `resolve` on a module tree for `agent`'s roles.yaml column, the
    generic one when None)."""
    column = agent or DEFAULT_AGENT
    payload, problems = (
        _module_payload(ws, column) if is_module_tree(ws) else _yaml_payload(ws)
    )
    if payload is None:
        return None, problems
    policy_set, problems = _load(payload)
    return (None, problems) if problems else (Policy(payload, policy_set, agent), [])


def policy_columns(
    ws: Path, agent: str | None, policy: Policy
) -> tuple[dict[str, Policy], list[str]]:
    """The policies a case's dry-runs must hold on, keyed by roles.yaml column,
    or none and why when a column is invalid (so no column passes unrun).

    A case for one agent, or a single policy.yaml, has one: `policy`. A role or
    project-wide case on a module tree must hold on the generic column and on
    every named agent's: a named cell replaces "*" for that agent, so an edit
    to "*" alone does nothing for an agent that has its own cell.
    """
    columns = {agent or DEFAULT_AGENT: policy}
    if agent is not None or not is_module_tree(ws):
        return columns, []
    named = {name for cells in (load_roles(ws) or {}).values() for name in cells}
    problems = []
    for name in sorted(named - set(columns)):
        column, column_problems = effective_policy(ws, name)
        if column is None:
            problems += [f"column {name}: {p}" for p in column_problems]
        else:
            columns[name] = column
    return ({}, problems) if problems else (columns, [])
