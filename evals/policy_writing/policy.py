"""Load, validate and dry-run a policy the way `hexgate policy` does, via the SDK.

`effective_policy` runs what `validate` (single file) or `check` + `resolve`
(module tree) runs, failing on lint warnings; `decide` runs what `test` runs,
with the CLI's input checks.
"""

from __future__ import annotations

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
from hexgate.security.analyzer import SEVERITY_RANK, check_default_role_exposure
from hexgate.security.constraints import ConstraintParseError
from hexgate.security.modules import DEFAULT_AGENT
from hexgate.security.testing import run_namespace

# Everything the SDK raises for a policy it can't load, compile or link.
POLICY_ERRORS = (PolicySetError, ConstraintParseError, LinkError, ValidationError)


@dataclass
class Policy:
    """The policy the checks run against.

    `payload` is the document: `policy.yaml` as written, or a module tree's
    resolved roles. `policy_set` is it loaded, ready to evaluate.
    """

    payload: dict
    policy_set: PolicySet


OUTCOMES = {
    DecisionOutcome.ALLOW: "allow",
    DecisionOutcome.DENY: "deny",
    DecisionOutcome.NEEDS_APPROVAL: "approval_required",
}

_ATTRIBUTES = TypeAdapter(dict[str, ContextAttributeValue])


def decide(policy: Policy, role: str, d: dict) -> tuple[str, str]:
    """Dry-run one call: (outcome, reason). Same inputs as `hexgate policy test`.

    An undefined role is an error rather than the `default` fallback, as in the
    CLI: a case naming a role the policy lacks fails instead of passing by luck.
    """
    if role not in policy.policy_set:
        return "error", f"role {role!r} not in policy ({policy.policy_set.roles})"
    try:
        attributes = _ATTRIBUTES.validate_python(d.get("attributes") or {})
        # Over a zeroed run, so an unset `run.*` path reads 0, not missing.
        run = run_namespace(d["tool"], **(d.get("run_facts") or {}))
    except (ValidationError, ValueError) as exc:
        return "error", str(exc)
    verdict = policy.policy_set.evaluate(
        role=role,
        tool=d["tool"],
        args=d.get("args") or {},
        attributes=attributes,
        run=run,
    )
    reason = "; ".join([verdict.reason, *map(str, verdict.violations or [])])
    return OUTCOMES[verdict.outcome], reason


def _lint_failures(lints) -> list[str]:
    # A warning fails, not only an error: the write-policy skill tells the agent
    # to validate with `--max-severity warning` (for `policy check` on a module
    # tree it doesn't yet; the spec aligns the skill in PR 11).
    return [
        f"[{lint.code}] {lint.message}"
        for lint in lints
        if SEVERITY_RANK[lint.severity] <= SEVERITY_RANK["warning"]
    ]


def _load(payload: dict) -> tuple[PolicySet | None, list[str]]:
    """What `hexgate policy validate` checks: load, compile, lint the roles."""
    try:
        policy_set = load_policy_set_from_dict(payload)
        # Compile too, so a policy the build would reject doesn't pass.
        compile_to_rego(payload)
    except POLICY_ERRORS as exc:
        return None, [str(exc)]
    return policy_set, _lint_failures(check_default_role_exposure(policy_set))


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


def effective_policy(
    ws: Path, agent: str = DEFAULT_AGENT
) -> tuple[Policy | None, list[str]]:
    """The policy in `ws`, or why it doesn't validate (`hexgate policy validate`,
    or `check` + `resolve` on a module tree for `agent`'s roles.yaml column)."""
    if (ws / "policies").is_dir():
        payload, problems = _module_payload(ws, agent)
    else:
        try:
            payload = yaml.safe_load((ws / "policy.yaml").read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            payload, problems = None, [str(exc)]
        else:
            if not isinstance(payload, dict):
                payload, problems = None, ["policy.yaml is not a YAML mapping"]
            else:
                problems = []
    if payload is not None:
        policy_set, problems = _load(payload)
    if problems:
        return None, problems
    return Policy(payload, policy_set), []
