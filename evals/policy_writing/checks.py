"""Score a policy-writing workspace: what the agent (or one of our answers) left behind.

No eval framework is imported here: the framework's scorer and the dataset tests
both call `score(case, workspace, before, answer)` and get back a list of `Check`s.
One function per kind of check: the policy validates without lint warnings
(`policy.py`); dry-run decisions and role supersets hold (for a role or
project-wide case, an allow for one agent that can make the call and anything
else for every agent that might); only names in the project's agents.json and audit.json
are used (the SDK's drift lints, and `names.py` for caller attributes); files
change (or not) as the case says; and the final answer mentions what the case
requires.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from evals.policy_writing.names import unknown_attrs
from evals.policy_writing.policy import (
    LABELS,
    RANK,
    CaseError,
    Policy,
    agent_policies,
    can_call,
    decide,
    dump_json,
    effective_policy,
    outcome,
)
from evals.policy_writing.sources import (
    NAME_SOURCES,
    ProjectAgents,
    SourceError,
    load_attributes,
    load_project_agents,
)
from hexgate.security.decision import Verdict

# The longest a check's detail gets: enough to read, short enough for a report.
DETAIL_MAX_CHARS = 800


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


def decision_checks(
    policies: dict[str, Policy],
    decisions: list[dict],
    agents: Collection[str] | None = None,
) -> list[Check]:
    """One check per (decision, role): the dry-run gives an expected outcome on
    the policies in `policies`, one per agent running it (see `agent_policies`);
    none means the policy is invalid. In a role-wide case (no agent, on a
    module tree), an expectation that accepts allow is met once one agent that
    can make the call (`can_call`) gets an accepted outcome; any other must hold
    for every agent that might (`_may_call`): one that can, one registered with
    no manifest, and `"*"` (an agent not registered yet), so none is left open.

    Named by the decision's place in the list too, so two entries that differ
    only in `expect`, or a call listed twice, still get one check each."""
    # `roles` expands one entry over several roles.
    return [
        _decision_check(
            f"decision {i}: {_call_label(role, d)}", policies, role, d, agents
        )
        for i, d in enumerate(decisions, 1)
        for role in d.get("roles") or [d["role"]]
    ]


def _decision_check(
    name: str,
    policies: dict[str, Policy],
    role: str,
    d: dict,
    agents: Collection[str] | None,
) -> Check:
    """One decision, by the rule in `decision_checks`."""
    labels = _labels(d)
    if agents is None or not policies:
        return _per_agent_check(
            name, policies, lambda policy: _decision_miss(policy, role, d, labels)
        )
    if failed := _uncallable(name, policies, [d["tool"]], agents):
        return failed
    able = {a: p for a, p in policies.items() if can_call(p, d["tool"], agents)}
    if "allow" in labels:
        return _per_agent_check(
            name,
            able,
            lambda policy: _decision_miss(policy, role, d, labels),
            any_one=True,
            tagged=True,
        )
    # An agent whose manifest rules the call out never makes it, so it isn't
    # judged; one with no manifest ("*", or registered without a version) might.
    judged = {a: p for a, p in policies.items() if _may_call(p, d["tool"], agents)}
    return _per_agent_check(
        name,
        judged,
        lambda policy: _decision_miss(policy, role, d, labels),
        tagged=True,
    )


def _may_call(policy: Policy, tool: str, agents: Collection[str]) -> bool:
    """Whether the agent `policy` runs for might make a call on `tool`: its
    manifest says it can, or it has none to say it can't (`"*"`, or an agent
    registered without a version, which the SDK reads as unknown too)."""
    return policy.manifest is None or can_call(policy, tool, agents)


def _uncallable(
    name: str, policies: dict[str, Policy], tools: list[str], agents: Collection[str]
) -> Check | None:
    """A failed check if no registered agent (`agents`) can call one of `tools`:
    a case about a call no agent makes says nothing about the policy."""
    nobody = [
        t for t in tools if not any(can_call(p, t, agents) for p in policies.values())
    ]
    if nobody:
        return Check(
            name, False, f"no agent in agents.json can call {', '.join(nobody)}"
        )
    return None


def _per_agent_check(
    name: str,
    policies: dict[str, Policy],
    misses: Callable[[Policy], list[str]],
    any_one: bool = False,
    tagged: bool = False,
) -> Check:
    """Passes when no policy in `policies` has a miss, or with `any_one` when
    one doesn't; each miss is tagged with its agent when `tagged`."""
    if not policies:
        return Check(name, False, "policy invalid")
    found = {agent: misses(policy) for agent, policy in policies.items()}
    detail = [
        f"{miss} (agent {agent})" if tagged else miss
        for agent, missed in found.items()
        for miss in missed
    ]
    passed = (any if any_one else all)(not missed for missed in found.values())
    return Check(name, passed, "" if passed else "; ".join(detail)[:DETAIL_MAX_CHARS])


def _labels(d: dict) -> list[str]:
    """The outcomes `d` accepts: `expect` may list several ("deny or
    approval_required")."""
    return d["expect"] if isinstance(d["expect"], list) else [d["expect"]]


def _decision_miss(policy: Policy, role: str, d: dict, labels: list[str]) -> list[str]:
    """Why the dry-run gives none of the outcomes in `labels`, if it does."""
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


def superset_checks(
    policies: dict[str, Policy],
    supersets: list[dict],
    agents: Collection[str] | None = None,
) -> list[Check]:
    """Everything `narrower` may do, `wider` may do at least as freely, for every
    agent in `policies`; in a role-wide case (`agents`, the registered ones), a
    registered agent only on the probes it can make (`"*"`, an agent not
    registered yet, on all of them)."""
    return [
        _superset_check(
            f"superset {i}: {s['wider']} ⊇ {s['narrower']}", policies, s, agents
        )
        for i, s in enumerate(supersets, 1)
    ]


def _superset_check(
    name: str, policies: dict[str, Policy], s: dict, agents: Collection[str] | None
) -> Check:
    if agents is None or not policies:
        return _per_agent_check(
            name, policies, lambda policy: _superset_gaps(policy, s, None)
        )
    tools = [p["tool"] for p in s["probes"]]
    if failed := _uncallable(name, policies, tools, agents):
        return failed
    return _per_agent_check(
        name, policies, lambda policy: _superset_gaps(policy, s, agents), tagged=True
    )


def _superset_gaps(
    policy: Policy, s: dict, agents: Collection[str] | None
) -> list[str]:
    """How `wider` is stricter than `narrower`, probe by probe; with `agents`
    (a role-wide case), a registered agent skips the probes it can't make."""
    return [
        gap
        for p in s["probes"]
        if agents is None or _may_call(policy, p["tool"], agents)
        if (gap := _probe_gap(policy, s, p))
    ]


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
    policy: Policy,
    ws: Path,
    before: dict[str, str],
    after: dict[str, str],
    resolved: list[Policy] | None = None,
    project: ProjectAgents | None = None,
) -> list[Check]:
    """Only names in agents.json and audit.json: the SDK's drift lints (`policy.drift`) for
    tools, skills, guards and arguments (a module tree's against every agent's
    manifest), and `policy.agent`'s caller attributes against audit.json, on
    its resolved roles only: `resolved`, the policy of every agent a role or
    project-wide case runs on (`policy` alone by default). Such a case has no agent, so
    any agent's attributes count. `project` is agents.json as `score` read it on a
    module tree, None for a policy.yaml."""
    # The names are read after the run, so an edit to either file could
    # whitelist an invented name: trust them only if they are untouched.
    edited = [f for f in NAME_SOURCES if before.get(f) != after.get(f)]
    if edited:
        return _name_checks_failed(f"edited during the run: {edited}")
    if policy.agent is None and project is None:
        # A single policy.yaml is one agent's: its names need that agent.
        return _name_checks_failed("a case without an agent needs a module tree")
    if policy.agent is not None and policy.manifest is None:
        return _name_checks_failed(
            f"agents.json has no readable manifest for {policy.agent!r}"
        )
    # With no manifest at all, the drift lints check nothing.
    if policy.agent is None and project is not None and not project.manifests:
        return _name_checks_failed("no agent in agents.json has a manifest")
    try:
        attrs = load_attributes(ws, policy.agent)
    except SourceError as exc:
        return _name_checks_failed(f"audit.json unreadable: {exc}"[:300])
    keys = _lines(x for x in policy.drift if x.code != "unknown-arg")
    args = _lines(x for x in policy.drift if x.code == "unknown-arg")
    refs = {
        ref for p in resolved or [policy] for ref in unknown_attrs(p.policy_set, attrs)
    }
    args += [
        f"[unknown-attribute] {ref}: no audit.json row sends it" for ref in sorted(refs)
    ]
    return [
        Check(name, not found, "\n".join(found)[:DETAIL_MAX_CHARS])
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
    agent = case.get("agent")  # none for a role or project-wide edit
    role_wide = agent is None and modules
    policy, problems = effective_policy(ws, agent, modules)
    policies: dict[str, Policy] = {}
    if policy is not None:
        policies, problems = agent_policies(ws, agent, policy, role_wide)
    # agents.json on a module tree: it has been read without error by now.
    project = load_project_agents(ws) if modules and policy else None
    agents = project.registered if role_wide and policies else None
    valid = Check("valid", not problems, "\n".join(problems)[:DETAIL_MAX_CHARS])
    after = snapshot(ws)
    return [
        valid,
        *decision_checks(policies, expect.get("decisions", []), agents),
        *superset_checks(policies, expect.get("superset", []), agents),
        # An invalid agent policy leaves none to dry-run, but the names still count.
        *(
            name_checks(policy, ws, before, after, list(policies.values()), project)
            if policy
            else []
        ),
        *file_checks(expect, before, after),
        *answer_checks(expect, answer),
    ]
