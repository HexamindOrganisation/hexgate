"""Score a policy-writing workspace: what the agent (or one of our answers) left behind.

No eval framework is imported here: the framework's scorer and the dataset tests
both call `score(case, workspace, before, answer)` and get back a list of `Check`s.
One function per kind of check: the policy validates without lint warnings
(`policy.py`), dry-run decisions and role supersets hold, files change (or not)
as the case says, and the final answer mentions what the case requires.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from evals.policy_writing.policy import (
    LABELS,
    RANK,
    CaseError,
    Policy,
    decide,
    effective_policy,
    outcome,
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
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() and not any(part.startswith(".") for part in rel.parts):
            files[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return files


def decision_checks(policy: Policy | None, decisions: list[dict]) -> list[Check]:
    """One check per (decision, role): the dry-run gives an expected outcome."""
    checks = []
    for d in decisions:
        # `roles` expands one entry over several roles; `expect` may list the
        # acceptable outcomes ("deny or approval_required").
        labels = d["expect"] if isinstance(d["expect"], list) else [d["expect"]]
        for role in d.get("roles") or [d["role"]]:
            name = f"decision: {_call_label(role, d)}"
            if policy is None:
                checks.append(Check(name, False, "policy invalid"))
                continue
            try:
                wanted = {outcome(label) for label in labels}
                verdict = decide(policy, role, d)
            except CaseError as exc:
                checks.append(Check(name, False, f"can't dry-run: {exc}"))
                continue
            if verdict.outcome in wanted:
                checks.append(Check(name, True))
                continue
            got = LABELS[verdict.outcome]
            detail = f"expected {' or '.join(labels)}, got {got}: {_reason(verdict)}"
            checks.append(Check(name, False, detail[:400]))
    return checks


def _reason(verdict: Verdict) -> str:
    return "; ".join([verdict.reason, *map(str, verdict.violations or [])])


def _call_label(role: str, d: dict) -> str:
    label = f"{role} → {d['tool']}({json.dumps(d.get('args') or {}, sort_keys=True, default=str)})"
    if d.get("attributes"):
        label += f" ctx={json.dumps(d['attributes'], sort_keys=True, default=str)}"
    if d.get("run_facts"):
        label += f" run={json.dumps(d['run_facts'], sort_keys=True, default=str)}"
    return label


def superset_checks(policy: Policy | None, supersets: list[dict]) -> list[Check]:
    """Everything `narrower` may do, `wider` may do at least as freely."""
    checks = []
    for s in supersets:
        name = f"superset: {s['wider']} ⊇ {s['narrower']}"
        if policy is None:
            checks.append(Check(name, False, "policy invalid"))
            continue
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
        checks.append(Check(name, not worse, "; ".join(worse)))
    return checks


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
    for key, name in (
        ("mentions_any", "answer mentions one of"),
        ("mentions_all", "answer mentions all of"),
    ):
        words = expect.get(key)
        if not words:
            continue
        if not isinstance(words, list):
            # A bare string would be checked letter by letter and pass by luck.
            checks.append(Check(name, False, f"{key} must be a list, got {words!r}"))
            continue
        # str(): YAML reads an unquoted 500 as a number.
        found = [w for w in words if str(w).lower() in text]
        if key == "mentions_any":
            detail = f"found {found}" if found else f"none of {words}"
            checks.append(Check(name, bool(found), detail))
        else:
            missing = [w for w in words if w not in found]
            checks.append(
                Check(name, not missing, f"missing {missing}" if missing else "")
            )
    return checks


def score(case: dict, ws: Path, before: dict[str, str], answer: str) -> list[Check]:
    expect = case.get("expect", {})
    policy, problems = effective_policy(ws, case["agent"])
    valid = Check("valid", not problems, "\n".join(problems)[-800:])
    after = snapshot(ws)
    return [
        valid,
        *decision_checks(policy, expect.get("decisions", [])),
        *superset_checks(policy, expect.get("superset", [])),
        *file_checks(expect, before, after),
        *answer_checks(expect, answer),
    ]
