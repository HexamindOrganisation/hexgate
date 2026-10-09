"""Score a policy-writing workspace: what the agent (or one of our answers) left behind.

No eval framework is imported here: the framework's scorer and the dataset tests
both call `score(case, workspace, before, answer)` and get back a list of `Check`s.
One function per kind of check: the policy validates without lint warnings
(`policy.py`), dry-run decisions and role supersets hold, only names in the
project's agents.json and audit.json are used (the SDK's drift lints, and `names.py` for caller
attributes), files change (or not)
as the case says, and the final answer mentions what the case requires.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from evals.policy_writing.names import unknown_attrs
from evals.policy_writing.policy import (
    LABELS,
    RANK,
    CaseError,
    Policy,
    decide,
    dump_json,
    effective_policy,
    outcome,
)
from evals.policy_writing.sources import (
    NAME_SOURCES,
    SourceError,
    load_attributes,
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


def decision_checks(policy: Policy | None, decisions: list[dict]) -> list[Check]:
    """One check per (decision, role): the dry-run gives an expected outcome.

    Named by the decision's place in the list too, so two entries that differ
    only in `expect`, or a call listed twice, still get one check each."""
    # `roles` expands one entry over several roles.
    return [
        _decision_check(policy, role, d, f"decision {i}: {_call_label(role, d)}")
        for i, d in enumerate(decisions, 1)
        for role in d.get("roles") or [d["role"]]
    ]


def _decision_check(policy: Policy | None, role: str, d: dict, name: str) -> Check:
    if policy is None:
        return Check(name, False, "policy invalid")
    # `expect` may list the acceptable outcomes ("deny or approval_required").
    labels = d["expect"] if isinstance(d["expect"], list) else [d["expect"]]
    try:
        verdict = decide(policy, role, d)
    except CaseError as exc:
        return Check(name, False, f"can't dry-run: {exc}")
    if verdict.outcome in {outcome(label) for label in labels}:
        return Check(name, True)
    got = LABELS[verdict.outcome]
    detail = f"expected {' or '.join(labels)}, got {got}: {_reason(verdict)}"
    return Check(name, False, detail[:400])


def _reason(verdict: Verdict) -> str:
    return "; ".join([verdict.reason, *map(str, verdict.violations or [])])


def _call_label(role: str, d: dict) -> str:
    label = f"{role} → {d['tool']}({dump_json(d.get('args', {}))})"
    if d.get("attributes"):
        label += f" ctx={dump_json(d['attributes'])}"
    if d.get("run_facts"):
        label += f" run={dump_json(d['run_facts'])}"
    return label


def superset_checks(policy: Policy | None, supersets: list[dict]) -> list[Check]:
    """Everything `narrower` may do, `wider` may do at least as freely."""
    return [
        _superset_check(policy, s, f"superset {i}: {s['wider']} ⊇ {s['narrower']}")
        for i, s in enumerate(supersets, 1)
    ]


def _superset_check(policy: Policy | None, s: dict, name: str) -> Check:
    if policy is None:
        return Check(name, False, "policy invalid")
    worse = [gap for p in s["probes"] if (gap := _probe_gap(policy, s, p))]
    return Check(name, not worse, "; ".join(worse))


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


def _untagged(lint) -> str:
    """`lint` without its role, so one module mistake reads once; the agent
    stays, as "the agent's manifest" in a named column's message means it."""
    return (
        f"[{lint.code}]"
        + (f" [agent {lint.agent}]" if lint.agent else "")
        + f" {lint.message}"
    )


def _lines(lints) -> list[str]:
    """Each lint as one line, once: a module's mistake is linted once per cell
    it's in."""
    return list(dict.fromkeys(_untagged(x) for x in lints))


def name_checks(
    policy: Policy, ws: Path, before: dict[str, str], after: dict[str, str]
) -> list[Check]:
    """Only names in agents.json and audit.json: the SDK's drift lints (`policy.drift`) for
    tools, skills, guards and arguments (a module tree's against every agent's
    manifest), and `policy.agent`'s caller attributes against audit.json, on
    its resolved roles only."""
    # The names are read after the run, so an edit to either file could
    # whitelist an invented name: trust them only if they are untouched.
    edited = [f for f in NAME_SOURCES if before.get(f) != after.get(f)]
    if edited:
        return _name_checks_failed(f"edited during the run: {edited}")
    if policy.manifest is None:
        return _name_checks_failed(
            f"agents.json has no readable manifest for {policy.agent!r}"
        )
    try:
        attrs = load_attributes(ws, policy.agent)
    except SourceError as exc:
        return _name_checks_failed(f"audit.json unreadable: {exc}"[:300])
    keys = _lines(x for x in policy.drift if x.code != "unknown-arg")
    args = _lines(x for x in policy.drift if x.code == "unknown-arg")
    args += [
        f"[unknown-attribute] {ref}: no audit.json row sends it"
        for ref in unknown_attrs(policy.policy_set, attrs)
    ]
    return [
        Check(name, not found, "\n".join(found)[:800])
        for name, found in zip(NAME_CHECKS, (keys, args), strict=True)
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
    policy, problems = effective_policy(ws, case["agent"], modules)
    valid = Check("valid", not problems, "\n".join(problems)[:800])
    after = snapshot(ws)
    return [
        valid,
        *decision_checks(policy, expect.get("decisions", [])),
        *superset_checks(policy, expect.get("superset", [])),
        *(name_checks(policy, ws, before, after) if policy else []),
        *file_checks(expect, before, after),
        *answer_checks(expect, answer),
    ]
