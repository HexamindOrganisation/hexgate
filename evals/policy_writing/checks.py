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

import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from evals.policy_writing.names import (
    NAME_SOURCES,
    enforced_roles,
    load_known_names,
    module_invented_names,
    unknown_keys,
    unknown_refs,
)
from evals.policy_writing.policy import (
    LABELS,
    RANK,
    CaseError,
    Policy,
    decide,
    dump_json,
    effective_policy,
    outcome,
    policy_columns,
)
from hexgate.security.decision import Verdict


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
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # never entered
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath, name)
            # Regular files only: a broken symlink or a FIFO would crash or hang the read.
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def decision_checks(columns: dict[str, Policy], decisions: list[dict]) -> list[Check]:
    """One check per (decision, role): the dry-run gives an expected outcome on
    every column the case holds on (see `policy_columns`; none: the policy is
    invalid).

    Named by the decision's place in the list too, so two entries that differ
    only in `expect`, or a call listed twice, still get one check each."""
    # `roles` expands one entry over several roles.
    return [
        _decision_check(columns, role, d, f"decision {i}: {_call_label(role, d)}")
        for i, d in enumerate(decisions, 1)
        for role in d.get("roles") or [d["role"]]
    ]


def _decision_check(columns: dict[str, Policy], role: str, d: dict, name: str) -> Check:
    return _column_check(name, columns, lambda p: _wrong_decision(p, role, d))


def _column_check(
    name: str, columns: dict[str, Policy], misses: Callable[[Policy], list[str]]
) -> Check:
    """Passes when `misses(policy)` is empty on every column; each miss is
    tagged with its column when there are several. No columns: invalid."""
    if not columns:
        return Check(name, False, "policy invalid")
    wrong = []
    for column, policy in columns.items():
        where = "" if len(columns) == 1 else f" (column {column})"
        wrong += [f"{miss}{where}" for miss in misses(policy)]
    return Check(name, not wrong, "; ".join(wrong))


def _wrong_decision(policy: Policy, role: str, d: dict) -> list[str]:
    """Why the dry-run gives none of the outcomes `d` expects; empty if it gives one."""
    # `expect` may list the acceptable outcomes ("deny or approval_required").
    labels = d["expect"] if isinstance(d["expect"], list) else [d["expect"]]
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


def _call_label(role: str, d: dict) -> str:
    label = f"{role} → {d['tool']}({dump_json(d.get('args', {}))})"
    if d.get("attributes"):
        label += f" ctx={dump_json(d['attributes'])}"
    if d.get("run_facts"):
        label += f" run={dump_json(d['run_facts'])}"
    return label


def superset_checks(columns: dict[str, Policy], supersets: list[dict]) -> list[Check]:
    """Everything `narrower` may do, `wider` may do at least as freely, per column."""
    return [
        _superset_check(columns, s, f"superset {i}: {s['wider']} ⊇ {s['narrower']}")
        for i, s in enumerate(supersets, 1)
    ]


def _superset_check(columns: dict[str, Policy], s: dict, name: str) -> Check:
    return _column_check(name, columns, lambda p: _probe_gaps(p, s))


def _probe_gaps(policy: Policy, s: dict) -> list[str]:
    """The probes `wider` lets through less freely than `narrower` on `policy`."""
    return [gap for p in s["probes"] if (gap := _probe_gap(policy, s, p))]


def _probe_gap(policy: Policy, s: dict, p: dict) -> str | None:
    """How `wider` is stricter than `narrower` on probe `p`, if it is."""
    try:
        lo = decide(policy, s["narrower"], p).outcome
        hi = decide(policy, s["wider"], p).outcome
    except CaseError as exc:  # e.g. a missing role: the probe fails
        return f"{p['tool']}: can't dry-run: {exc}"
    if RANK[hi] < RANK[lo]:
        return f"{p['tool']}: {s['narrower']}={LABELS[lo]}, {s['wider']}={LABELS[hi]}"
    return None


NAME_CHECKS = (
    "only known tools, skills and guards",
    "only known arguments and attributes",
)


def _name_checks_failed(detail: str) -> list[Check]:
    return [Check(name, False, detail) for name in NAME_CHECKS]


def name_checks(
    policy: Policy,
    ws: Path,
    before: dict[str, str],
    after: dict[str, str],
    modules: bool = False,
) -> list[Check]:
    """Only names the MCP would show for `policy.agent` (any agent's when None):
    tools, skills, guards, reach targets, arguments and caller attributes. A
    single policy.yaml is checked as a whole, a module tree (`modules`) file by
    file."""
    # The names are read after the run, so an edit to either file could
    # whitelist an invented name: trust them only if they are untouched.
    edited = [f for f in NAME_SOURCES if before.get(f) != after.get(f)]
    if edited:
        return _name_checks_failed(f"edited during the run: {edited}")
    try:
        known = load_known_names(ws, policy.agent)
        if modules:
            unknown, refs = module_invented_names(ws, policy.agent, known)
        else:
            roles = enforced_roles(policy.policy_set)
            unknown, refs = unknown_keys(roles, known), unknown_refs(roles, known)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _name_checks_failed(
            f"agents.json / audit.json unreadable: {exc!r}"[:300]
        )
    keys, args = NAME_CHECKS
    agent = policy.agent
    where = f"{agent}'s manifest" if agent else "any agent's manifest"
    return [
        Check(keys, not unknown, f"not in {where}: {unknown}" if unknown else ""),
        Check(
            args, not refs, f"not in the manifest or audit.json: {refs}" if refs else ""
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
    # A path in neither snapshot is a typo in the case, not an untouched file.
    # Deleting a file changes it; the dry-runs judge what that did to the policy.
    for kind, want_same in [("changed", False), ("unchanged", True)]:
        for rel in expect.get(kind, []):
            name = f"{kind}: {rel}"
            if rel not in before and rel not in after:
                checks.append(Check(name, False, "no such file"))
            else:
                same = after.get(rel) == before.get(rel)
                checks.append(Check(name, same == want_same))
    return checks


# What a word is made of; `_` is a separator, as in snake_case names. A `,` or
# `.` between digits is part of a number: "500" isn't in "1,500" or "500.5".
_ALNUM = "[A-Za-z0-9]"
_START = rf"(?<!{_ALNUM})(?<![0-9][.,])"
_END = rf"(?!{_ALNUM})(?![.,][0-9])"


def _mentions(answer: str, word: str) -> bool:
    """`word` as a whole word or phrase: "no" doesn't match "know", while
    "approval" still matches inside `approval_required`. Inflections don't
    match ("refund" vs "refunds"), so a case lists each form it accepts."""
    pattern = rf"{_START}{re.escape(word)}{_END}"
    return re.search(pattern, answer, re.IGNORECASE) is not None


def answer_checks(expect: dict, answer: str) -> list[Check]:
    """`mentions_any` and `mentions_all`, case-insensitive whole words, on the answer."""
    checks = []
    if words := expect.get("mentions_any"):
        hit = [w for w in words if _mentions(answer, w)]
        detail = f"found {hit}" if hit else f"none of {words}"
        checks.append(Check("answer mentions one of", bool(hit), detail))
    if words := expect.get("mentions_all"):
        missing = [w for w in words if not _mentions(answer, w)]
        detail = f"missing {missing}" if missing else ""
        checks.append(Check("answer mentions all of", not missing, detail))
    return checks


def score(case: dict, ws: Path, before: dict[str, str], answer: str) -> list[Check]:
    """Every check for `case`, as the case loader returns it (PR 2), which has
    already validated its shape: calls are mappings, outcomes and mention lists
    well-formed. `before` is the starting project's `snapshot`; its layout, not
    the workspace's, says whether the policy is `policy.yaml` or a module tree,
    so a stray `policies/` the agent made doesn't switch it."""
    expect = case.get("expect", {})
    modules = any(rel.startswith("policies/") for rel in before)
    agent = case.get("agent")
    policy, problems = effective_policy(ws, agent, modules)
    columns: dict[str, Policy] = {}
    if policy is not None:
        columns, problems = policy_columns(ws, agent, policy, modules)
    valid = Check("valid", not problems, "\n".join(problems)[:800])
    after = snapshot(ws)
    return [
        valid,
        *decision_checks(columns, expect.get("decisions", [])),
        *superset_checks(columns, expect.get("superset", [])),
        *(name_checks(policy, ws, before, after, modules) if policy else []),
        *file_checks(expect, before, after),
        *answer_checks(expect, answer),
    ]
