"""Score a policy-writing workspace: what the agent (or one of our answers) left behind.

No eval framework is imported here: the framework's scorer and the dataset tests
both call `score(case, workspace, before, answer)` and get back a list of `Check`s.
The checks call the SDK functions the platform's policy endpoints use (load,
compile, lint, resolve, evaluate): the policy must validate without lint
warnings, every dry-run decision must match, files must change (or not) as the
case says, only tools and arguments `TOOLS.md` lists may be used, and the final
answer must mention what the case requires.
"""

from __future__ import annotations

import hashlib
import json
import re
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
from hexgate.security.models import AGENT_RUN_TOOL, agent_target_key
from hexgate.security.network import NET_HTTP_REQUEST, NET_TCP_CONNECT
from hexgate.security.testing import run_namespace

# Everything the SDK raises for a policy it can't load, compile or link.
POLICY_ERRORS = (PolicySetError, ConstraintParseError, LinkError, ValidationError)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def snapshot(root: Path) -> dict[str, str]:
    """{relative path: sha256} of every file, skipping any path with a dot component.

    A dot path is tooling, not project: the agent's skill sits under
    `.claude/skills/` in the workspace. Counting it would fail every
    `no_changes` case.
    """
    files = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() and not any(part.startswith(".") for part in rel.parts):
            files[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return files


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
RANK = {"deny": 0, "approval_required": 1, "allow": 2}


_ATTRIBUTES = TypeAdapter(dict[str, ContextAttributeValue])


def decide(policy: Policy, role: str, d: dict) -> tuple[str, str]:
    """Dry-run one call: (outcome, reason). Same inputs as `hexgate policy test`.

    An undefined role is an error rather than the `default` fallback, as in the
    CLI: a case naming a role the policy lacks fails instead of passing by luck.
    """
    if role not in policy.policy_set:
        return "error", f"role {role!r} not in policy ({policy.policy_set.roles})"
    try:
        attributes = _ATTRIBUTES.validate_python(d.get("attributes", {}))
        # Over a zeroed run, so an unset `run.*` path reads 0, not missing.
        run = run_namespace(d["tool"], **d.get("run_facts", {}))
    except (ValidationError, ValueError) as exc:
        return "error", str(exc)
    verdict = policy.policy_set.evaluate(
        role=role,
        tool=d["tool"],
        args=d.get("args", {}),
        attributes=attributes,
        run=run,
    )
    reason = "; ".join([verdict.reason, *map(str, verdict.violations or [])])
    return OUTCOMES[verdict.outcome], reason


# Arguments the synthetic keys carry, copied from the gates that build those calls
# (hexgate/egress/model.py and tcp.py, hexgate/security/agent_gate.py);
# tests/evals/test_checks.py fails if a gate's built arguments drift from these.
AGENT_REACH_ARGS = frozenset({"agent", "target", "via"})
SYNTHETIC_ARGS = {
    NET_HTTP_REQUEST: frozenset(
        {"method", "scheme", "host", "port", "url", "path", "query"}
    ),
    NET_TCP_CONNECT: frozenset({"host", "port", "protocol"}),
    AGENT_RUN_TOOL: frozenset({"agent"}),
}
REF = re.compile(r"\b(args|ctx)\.([A-Za-z_]\w*)")


def read_manifest(ws: Path) -> tuple[dict[str, set[str]], set[str]]:
    """TOOLS.md → ({tool: its argument names}, caller attribute names).

    Rows look like | `tool` | `arg: type`, ... |; the heading above a table
    says whether it lists tools or caller attributes.
    """
    tools: dict[str, set[str]] = {}
    attrs: set[str] = set()
    section = ""
    for line in (ws / "TOOLS.md").read_text().splitlines():
        if line.startswith("#"):
            section = line.lower()
        m = re.match(r"^\|\s*`([^`]+)`\s*\|([^|]*)", line)
        if not m:
            continue
        if "attribute" in section:
            attrs.add(m.group(1))
        elif "tool" in section:
            tools[m.group(1)] = set(re.findall(r"`(\w+)\s*:", m.group(2)))
    return tools, attrs


def policy_bodies(doc: dict) -> tuple[dict, list[dict]]:
    bodies = [b for b in (doc.get("roles") or {}).values() if isinstance(b, dict)]
    return doc, bodies or [doc]


def policy_tools(doc: dict) -> set[str]:
    _, bodies = policy_bodies(doc)
    return {t for b in bodies for t in (b.get("tools") or {})}


def unknown_refs(doc: dict, tools: dict[str, set[str]], attrs: set[str]) -> list[str]:
    """`args.x` / `ctx.x` a constraint uses that TOOLS.md doesn't define."""
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
        for target, spec in (b.get("agents") or {}).items():
            key = agent_target_key("tool", target)  # both vias carry the same args
            lines += [(key, c) for c in (spec or {}).get("constraints") or []]
        for tool, spec in (b.get("tools") or {}).items():
            lines += [(tool, c) for c in (spec or {}).get("constraints") or []]
    bad = set()
    for tool, text in lines:
        for kind, name in REF.findall(str(text)):
            if kind == "ctx":
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
                bad.add(f"{tool or 'policy-level'}: {kind}.{name}")
    return sorted(bad)


def _lint_failures(lints) -> list[str]:
    # A warning fails, not only an error: the write-policy skill tells the agent
    # to validate with `--max-severity warning`.
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


def _module_payload(ws: Path) -> tuple[dict | None, list[str]]:
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
        result = resolve_for_project(boundaries, capabilities, roles)
    except POLICY_ERRORS as exc:
        return None, [str(exc)]
    return {"roles": effective_policy_by_role(result), RESOLVED_POLICY_MARKER: True}, []


def effective_policy(ws: Path) -> tuple[Policy | None, Check]:
    if (ws / "policies").is_dir():
        payload, problems = _module_payload(ws)
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
        return None, Check("valid", False, "\n".join(problems)[-800:])
    return Policy(payload, policy_set), Check("valid", True)


def score(case: dict, ws: Path, before: dict[str, str], answer: str) -> list[Check]:
    expect = case.get("expect", {})
    policy, valid = effective_policy(ws)
    checks = [valid]

    for d in expect.get("decisions", []):
        # `roles` expands one entry over several roles; `expect` may list the
        # acceptable outcomes ("deny or approval_required").
        wanted = d["expect"] if isinstance(d["expect"], list) else [d["expect"]]
        for role in d.get("roles") or [d["role"]]:
            label = (
                f"{role} → {d['tool']}({json.dumps(d.get('args', {}), sort_keys=True)})"
            )
            if d.get("attributes"):
                label += f" ctx={json.dumps(d['attributes'], sort_keys=True)}"
            if d.get("run_facts"):
                label += f" run={json.dumps(d['run_facts'], sort_keys=True)}"
            if policy is None:
                checks.append(Check(f"decision: {label}", False, "policy invalid"))
                continue
            got, raw = decide(policy, role, d)
            ok = got in wanted
            detail = (
                "" if ok else f"expected {' or '.join(wanted)}, got {got}: {raw[:300]}"
            )
            checks.append(Check(f"decision: {label}", ok, detail))

    for s in expect.get("superset", []):
        # Everything `narrower` may do, `wider` may do at least as freely.
        name = f"superset: {s['wider']} ⊇ {s['narrower']}"
        if policy is None:
            checks.append(Check(name, False, "policy invalid"))
            continue
        worse = []
        for p in s["probes"]:
            lo, _ = decide(policy, s["narrower"], p)
            hi, _ = decide(policy, s["wider"], p)
            # A probe either role can't be evaluated on (a missing role) fails it.
            if "error" in (lo, hi) or RANK[hi] < RANK[lo]:
                worse.append(f"{p['tool']}: {s['narrower']}={lo}, {s['wider']}={hi}")
        checks.append(Check(name, not worse, "; ".join(worse)))

    if policy is not None:
        tools, attrs = read_manifest(ws)
        unknown = sorted(
            t
            for t in policy_tools(policy.payload)
            if t not in tools and not t.startswith(("net.", "agent."))
        )
        checks.append(
            Check(
                "only known tools",
                not unknown,
                f"not in TOOLS.md: {unknown}" if unknown else "",
            )
        )
        refs = unknown_refs(policy.payload, tools, attrs)
        checks.append(
            Check(
                "only known arguments and attributes",
                not refs,
                f"not in TOOLS.md: {refs}" if refs else "",
            )
        )

    after = snapshot(ws)
    if expect.get("no_changes"):
        diff = sorted(
            k for k in set(before) | set(after) if before.get(k) != after.get(k)
        )
        checks.append(Check("no changes", not diff, f"changed: {diff}" if diff else ""))
    for rel in expect.get("changed", []):
        checks.append(Check(f"changed: {rel}", after.get(rel) != before.get(rel)))
    for rel in expect.get("unchanged", []):
        checks.append(Check(f"unchanged: {rel}", after.get(rel) == before.get(rel)))

    text = answer.lower()
    words = expect.get("mentions_any")
    if words:
        hit = [w for w in words if w.lower() in text]
        checks.append(
            Check(
                "answer mentions one of",
                bool(hit),
                f"found {hit}" if hit else f"none of {words}",
            )
        )
    words = expect.get("mentions_all")
    if words:
        missing = [w for w in words if w.lower() not in text]
        checks.append(
            Check(
                "answer mentions all of",
                not missing,
                f"missing {missing}" if missing else "",
            )
        )
    return checks
