"""Score a policy-writing workspace: what the agent (or one of our answers) left behind.

No eval framework is imported here: the framework's scorer and the dataset tests
both call `score(case, workspace, before, answer)` and get back a list of `Check`s.
One function per kind of check: the policy validates without lint warnings
(`policy.py`), dry-run decisions and role supersets hold (on every roles.yaml
column when the case names no agent), only names the Hexgate MCP would show are
used (`names.py`: the case agent's, or any agent's when it names none; on a
module tree, per file), files change (or not) as the case says, and the final
answer mentions what the case requires.
"""

from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from evals.policy_writing.names import invented_names
from evals.policy_writing.policy import (
    LABELS,
    RANK,
    CaseError,
    Policy,
    decide,
    effective_policy,
    outcome,
    policy_columns,
)
from hexgate.security.decision import Verdict
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
        labels = d["expect"] if isinstance(d["expect"], list) else [d["expect"]]
        for role in d.get("roles") or [d["role"]]:
            name = f"decision: {_call_label(role, d)}"
            checks.append(
                _column_check(
                    name, columns, lambda p: _wrong_decision(p, role, d, labels)
                )
            )
    return checks


def _column_check(name: str, columns: dict[str, Policy], misses) -> Check:
    """Passes when `misses(policy)` is empty on every column; each miss is
    tagged with its column when there are several. No columns: invalid."""
    if not columns:
        return Check(name, False, "policy invalid")
    wrong = []
    for column, policy in columns.items():
        where = "" if len(columns) == 1 else f" (column {column})"
        wrong += [f"{miss}{where}" for miss in misses(policy)]
    return Check(name, not wrong, "; ".join(wrong))


def _wrong_decision(policy: Policy, role: str, d: dict, labels: list[str]) -> list[str]:
    """Why the dry-run gives none of the `labels` outcomes; empty if it gives one."""
    try:
        verdict = decide(policy, role, d)
    except CaseError as exc:
        return [f"can't dry-run: {exc}"]
    if verdict.outcome in {outcome(label) for label in labels}:
        return []
    got = LABELS[verdict.outcome]
    return [f"expected {' or '.join(labels)}, got {got}: {_reason(verdict)}"[:400]]


def _reason(verdict: Verdict) -> str:
    return "; ".join([verdict.reason, *map(str, verdict.violations or [])])


# default=str: a case loader keeps an unquoted YAML date as a date.
_dump = functools.partial(json.dumps, sort_keys=True, default=str)


def _call_label(role: str, d: dict) -> str:
    label = f"{role} → {d['tool']}({_dump(d.get('args', {}))})"
    if d.get("attributes"):
        label += f" ctx={_dump(d['attributes'])}"
    if d.get("run_facts"):
        label += f" run={_dump(d['run_facts'])}"
    return label


def superset_checks(columns: dict[str, Policy], supersets: list[dict]) -> list[Check]:
    """Everything `narrower` may do, `wider` may do at least as freely, per column."""
    checks = []
    for s in supersets:
        name = f"superset: {s['wider']} ⊇ {s['narrower']}"
        checks.append(_column_check(name, columns, lambda p: _worse_probes(p, s)))
    return checks


def _worse_probes(policy: Policy, s: dict) -> list[str]:
    """The probes `wider` lets through less freely than `narrower`."""
    worse = []
    for p in s["probes"]:
        try:
            lo = decide(policy, s["narrower"], p).outcome
            hi = decide(policy, s["wider"], p).outcome
        except CaseError as exc:  # e.g. a missing role: the probe fails
            worse.append(f"{p['tool']}: can't dry-run: {exc}")
            continue
        if RANK[hi] < RANK[lo]:
            worse.append(
                f"{p['tool']}: {s['narrower']}={LABELS[lo]}, {s['wider']}={LABELS[hi]}"
            )
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
    """Only tools, arguments and attributes the MCP would show (`invented_names`)."""
    # The names are read after the run, so an edit to either file could
    # whitelist an invented name: trust them only if they are untouched.
    edited = [f for f in ("agents.json", "audit.json") if before.get(f) != after.get(f)]
    if edited:
        return _name_checks_failed(f"edited during the run: {edited}")
    try:
        unknown, refs = invented_names(ws, policy.payload, agent)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _name_checks_failed(
            f"agents.json / audit.json unreadable: {exc!r}"[:300]
        )
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
    """Every check for `case`, as the case loader returns it (PR 2), which has
    already validated its shape: calls are mappings, outcomes and mention lists
    well-formed. `before` is the starting project's `snapshot`."""
    expect = case.get("expect", {})
    agent = case.get("agent")
    policy, problems = effective_policy(ws, agent or DEFAULT_AGENT)
    columns: dict[str, Policy] = {}
    if policy is not None:
        columns, problems = policy_columns(ws, agent, policy)
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
