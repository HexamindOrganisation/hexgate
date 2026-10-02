"""Score a policy-writing workspace: what the agent (or one of our answers) left behind.

No eval framework is imported here: the framework's scorer and the dataset tests
both call `score(case, workspace, before, answer)` and get back a list of `Check`s.
One function per kind of check: the policy validates without lint warnings
(`policy.py`), dry-run decisions and role supersets hold (on every roles.yaml
column when the case names no agent), only names the Hexgate MCP would show are
used (`names.py`: the case agent's, or any agent's when it names none; each
module file, any agent's), files change (or not) as the case says, and the final
answer mentions what the case requires.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from evals.policy_writing.names import (
    load_known_names,
    module_unknowns,
    org_wide_tools,
    unknown_refs,
    unknown_tools,
)
from evals.policy_writing.policy import (
    RANK,
    CaseError,
    Policy,
    decide,
    effective_policy,
    policy_columns,
)
from hexgate.security import load_local_modules, load_roles
from hexgate.security.modules import DEFAULT_AGENT


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


def decision_checks(columns: dict[str, Policy], decisions: list[dict]) -> list[Check]:
    """One check per (decision, role): the dry-run gives an expected outcome.

    `columns` are the policies it must hold on (see `policy_columns`); none
    means the policy is invalid.
    """
    checks = []
    for d in decisions:
        # `roles` expands one entry over several roles; `expect` may list the
        # acceptable outcomes ("deny or approval_required").
        wanted = d["expect"] if isinstance(d["expect"], list) else [d["expect"]]
        for role in d.get("roles") or [d["role"]]:
            name = f"decision: {_call_label(role, d)}"
            if not columns:
                checks.append(Check(name, False, "policy invalid"))
                continue
            wrong = []
            for column, policy in columns.items():
                if miss := _wrong_decision(policy, role, d, wanted):
                    where = "" if len(columns) == 1 else f" (column {column})"
                    wrong.append(f"{miss}{where}")
            checks.append(Check(name, not wrong, "; ".join(wrong)))
    return checks


def _wrong_decision(policy: Policy, role: str, d: dict, wanted: list[str]) -> str:
    """Why the dry-run misses `wanted`, or "" when it gives one of them."""
    try:
        got, reason = decide(policy, role, d)
    except CaseError as exc:
        return f"can't dry-run: {exc}"
    if got in wanted:
        return ""
    return f"expected {' or '.join(wanted)}, got {got}: {reason[:300]}"


def _call_label(role: str, d: dict) -> str:
    label = f"{role} → {d['tool']}({json.dumps(d.get('args') or {}, sort_keys=True)})"
    if d.get("attributes"):
        label += f" ctx={json.dumps(d['attributes'], sort_keys=True)}"
    if d.get("run_facts"):
        label += f" run={json.dumps(d['run_facts'], sort_keys=True)}"
    return label


def superset_checks(columns: dict[str, Policy], supersets: list[dict]) -> list[Check]:
    """Everything `narrower` may do, `wider` may do at least as freely, per column."""
    checks = []
    for s in supersets:
        name = f"superset: {s['wider']} ⊇ {s['narrower']}"
        if not columns:
            checks.append(Check(name, False, "policy invalid"))
            continue
        worse = []
        for column, policy in columns.items():
            where = "" if len(columns) == 1 else f" (column {column})"
            worse += [f"{w}{where}" for w in _worse_probes(policy, s)]
        checks.append(Check(name, not worse, "; ".join(worse)))
    return checks


def _worse_probes(policy: Policy, s: dict) -> list[str]:
    """The probes `wider` lets through less freely than `narrower`."""
    worse = []
    for p in s["probes"]:
        try:
            lo, _ = decide(policy, s["narrower"], p)
            hi, _ = decide(policy, s["wider"], p)
        except CaseError as exc:  # e.g. a missing role: the probe fails
            worse.append(f"{p['tool']}: can't dry-run: {exc}")
            continue
        if RANK[hi] < RANK[lo]:
            worse.append(f"{p['tool']}: {s['narrower']}={lo}, {s['wider']}={hi}")
    return worse


def _name_checks_failed(detail: str) -> list[Check]:
    return [
        Check("only known tools", False, detail),
        Check("only known arguments and attributes", False, detail),
    ]


def name_checks(
    policy: Policy,
    ws: Path,
    agent: str | None,
    before: dict[str, str],
    after: dict[str, str],
) -> list[Check]:
    """Only tools, arguments and attributes the MCP would show for `agent`
    (any agent's when None), and on a module tree, in every module file."""
    # The names are read after the run, so an edit to either file could
    # whitelist an invented name: trust them only if they are untouched.
    edited = [f for f in ("agents.json", "audit.json") if before.get(f) != after.get(f)]
    if edited:
        return _name_checks_failed(f"edited during the run: {edited}")
    try:
        tools, attrs = load_known_names(ws, agent)
        all_tools, all_attrs = (
            (tools, attrs) if agent is None else load_known_names(ws, None)
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _name_checks_failed(
            f"agents.json / audit.json unreadable: {exc!r}"[:300]
        )
    unknown = unknown_tools(policy.payload, tools)
    refs = unknown_refs(policy.payload, tools, attrs)
    if (ws / "policies").is_dir():
        # The policy validated, so the modules and roles.yaml load.
        boundaries, capabilities = load_local_modules(ws)
        exempt = org_wide_tools(
            boundaries, capabilities, load_roles(ws), agent or DEFAULT_AGENT
        )
        unknown = [t for t in unknown if t not in exempt]
        file_tools, file_refs = module_unknowns(
            ws, [*boundaries, *capabilities], all_tools, all_attrs
        )
        unknown += file_tools
        refs += file_refs
    where = f"{agent}'s manifest" if agent else "any agent's manifest"
    return [
        Check(
            "only known tools",
            not unknown,
            f"not in {where}: {unknown}" if unknown else "",
        ),
        Check(
            "only known arguments and attributes",
            not refs,
            f"not in the manifest or audit.json: {refs}" if refs else "",
        ),
    ]


def file_checks(
    expect: dict, before: dict[str, str], after: dict[str, str]
) -> list[Check]:
    """`no_changes`, `changed` and `unchanged`, from the two snapshots."""
    checks = []
    if expect.get("no_changes"):
        diff = sorted(
            k for k in set(before) | set(after) if before.get(k) != after.get(k)
        )
        checks.append(Check("no changes", not diff, f"changed: {diff}" if diff else ""))
    for rel in expect.get("changed", []):
        checks.append(Check(f"changed: {rel}", after.get(rel) != before.get(rel)))
    for rel in expect.get("unchanged", []):
        # A path in neither snapshot is a typo in the case, not an untouched file.
        if rel not in before and rel not in after:
            checks.append(Check(f"unchanged: {rel}", False, "no such file"))
        else:
            checks.append(Check(f"unchanged: {rel}", after.get(rel) == before.get(rel)))
    return checks


def answer_checks(expect: dict, answer: str) -> list[Check]:
    """`mentions_any` and `mentions_all`, case-insensitive, on the final answer."""
    text = answer.lower()
    checks = []
    if words := expect.get("mentions_any"):
        hit = [w for w in words if w.lower() in text]
        detail = f"found {hit}" if hit else f"none of {words}"
        checks.append(Check("answer mentions one of", bool(hit), detail))
    if words := expect.get("mentions_all"):
        missing = [w for w in words if w.lower() not in text]
        detail = f"missing {missing}" if missing else ""
        checks.append(Check("answer mentions all of", not missing, detail))
    return checks


def score(case: dict, ws: Path, before: dict[str, str], answer: str) -> list[Check]:
    expect = case.get("expect", {})
    agent = case.get("agent")
    policy, problems = effective_policy(ws, agent or DEFAULT_AGENT)
    columns: dict[str, Policy] = {}
    if policy is not None:
        columns, problems = policy_columns(ws, agent, policy)
        if problems:  # a column left out would let its dry-runs pass unrun
            columns = {}
    valid = Check("valid", not problems, "\n".join(problems)[-800:])
    after = snapshot(ws)
    return [
        valid,
        *decision_checks(columns, expect.get("decisions", [])),
        *superset_checks(columns, expect.get("superset", [])),
        *(name_checks(policy, ws, agent, before, after) if policy else []),
        *file_checks(expect, before, after),
        *answer_checks(expect, answer),
    ]
