"""Score a policy-writing workspace: what the agent (or one of our answers) left behind.

No eval framework is imported here: the framework's scorer and the dataset tests
both call `score(case, workspace, before, answer)` and get back a list of `Check`s.
One function per kind of check: the policy validates without lint warnings
(`policy.py`), dry-run decisions and role supersets hold, only names the Hexgate
MCP would show for the case's agent are used (`names.py`), files change (or not)
as the case says, and the final answer mentions what the case requires.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from evals.policy_writing.names import (
    SYNTHETIC_ARGS,
    load_known_names,
    policy_tools,
    unknown_refs,
)
from evals.policy_writing.policy import Policy, decide, effective_policy


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


RANK = {"deny": 0, "approval_required": 1, "allow": 2}


def decision_checks(policy: Policy | None, decisions: list[dict]) -> list[Check]:
    """One check per (decision, role): the dry-run gives an expected outcome."""
    checks = []
    for d in decisions:
        # `roles` expands one entry over several roles; `expect` may list the
        # acceptable outcomes ("deny or approval_required").
        wanted = d["expect"] if isinstance(d["expect"], list) else [d["expect"]]
        for role in d.get("roles") or [d["role"]]:
            name = f"decision: {_call_label(role, d)}"
            if policy is None:
                checks.append(Check(name, False, "policy invalid"))
                continue
            got, raw = decide(policy, role, d)
            ok = got in wanted
            detail = (
                "" if ok else f"expected {' or '.join(wanted)}, got {got}: {raw[:300]}"
            )
            checks.append(Check(name, ok, detail))
    return checks


def _call_label(role: str, d: dict) -> str:
    label = f"{role} → {d['tool']}({json.dumps(d.get('args', {}), sort_keys=True)})"
    if d.get("attributes"):
        label += f" ctx={json.dumps(d['attributes'], sort_keys=True)}"
    if d.get("run_facts"):
        label += f" run={json.dumps(d['run_facts'], sort_keys=True)}"
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
            lo, _ = decide(policy, s["narrower"], p)
            hi, _ = decide(policy, s["wider"], p)
            # A probe either role can't be evaluated on (a missing role) fails it.
            if "error" in (lo, hi) or RANK[hi] < RANK[lo]:
                worse.append(f"{p['tool']}: {s['narrower']}={lo}, {s['wider']}={hi}")
        checks.append(Check(name, not worse, "; ".join(worse)))
    return checks


def name_checks(
    policy: Policy, ws: Path, agent: str, before: dict[str, str], after: dict[str, str]
) -> list[Check]:
    """Only tools, arguments and attributes the MCP would show for `agent`."""
    # The names are read after the run, so an edit to either file could
    # whitelist an invented name: trust them only if they are untouched.
    edited = [f for f in ("agents.json", "audit.json") if before.get(f) != after.get(f)]
    problem = f"edited during the run: {edited}" if edited else ""
    if not problem:
        try:
            tools, attrs = load_known_names(ws, agent)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problem = f"agents.json / audit.json unreadable: {exc!r}"[:300]
    if problem:
        return [
            Check("only known tools", False, problem),
            Check("only known arguments and attributes", False, problem),
        ]
    unknown = sorted(
        t
        for t in policy_tools(policy.payload)
        if t not in tools and t not in SYNTHETIC_ARGS and not t.startswith("agent.")
    )
    refs = unknown_refs(policy.payload, tools, attrs)
    return [
        Check(
            "only known tools",
            not unknown,
            f"not in {agent}'s manifest: {unknown}" if unknown else "",
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
    policy, problems = effective_policy(ws, case["agent"])
    valid = Check("valid", not problems, "\n".join(problems)[-800:])
    after = snapshot(ws)
    return [
        valid,
        *decision_checks(policy, expect.get("decisions", [])),
        *superset_checks(policy, expect.get("superset", [])),
        *(name_checks(policy, ws, case["agent"], before, after) if policy else []),
        *file_checks(expect, before, after),
        *answer_checks(expect, answer),
    ]
