"""Load the policy-writing cases: one `case.yaml` per directory, grouped by category.

No eval framework is imported here. `load_cases` turns each case into a dict
that `checks.score` reads, with the checks every case shares already added:
the protected files must stay unchanged, and the starting project's
`preserve.yaml` calls must keep their result. `apply_solution` puts one of our
answers (`solution/` or `wrong_answer/`) into a workspace in place of an agent.
"""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from evals.policy_writing.calls import bad_values, complete, load_known, unknown_names
from evals.policy_writing.checks import snapshot
from evals.policy_writing.policy import CaseError, check_run_facts, dump_json

HERE = Path(__file__).resolve().parent

# Files in the starting project no case may change unless it lists them in
# `expect.changed`: what the platform reports about the agents (their manifests
# and audit rows), the security team's boundaries, and the other agents' policies.
PROTECTED = ("agents.json", "audit.json", "policies/boundaries/**", "other_agents/**")
PRESERVE = "preserve.yaml"
# What a case directory may hold (README, "A case"); dot entries are tooling.
CASE_ENTRIES = {"case.yaml", "starting_project", "solution", "wrong_answer"}


class _Strict(BaseModel):
    # An unknown key is an error, not ignored: a typo would otherwise silently
    # drop a check. Strict: `held_out: "yes"` is not a bool.
    model_config = ConfigDict(extra="forbid", strict=True)


Outcome = Literal["allow", "deny", "approval_required"]


class Probe(_Strict):
    tool: str
    args: dict[str, Any] = {}
    attributes: dict[str, Any] = {}
    run_facts: dict[str, Any] = {}


class Decision(Probe):
    """One dry-run call; `roles` repeats it, a list `expect` accepts any."""

    role: str | None = None
    roles: list[str] | None = Field(None, min_length=1)
    expect: Outcome | Annotated[list[Outcome], Field(min_length=1)]

    @model_validator(mode="after")
    def _one_of_role_and_roles(self):
        if (self.role is None) == (self.roles is None):
            raise ValueError("exactly one of `role` and `roles`")
        return self


class Superset(_Strict):
    wider: str
    narrower: str
    probes: list[Probe] = Field(min_length=1)


class Expect(_Strict):
    decisions: list[Decision] = []
    changed: list[str] = []
    unchanged: list[str] = []
    # Empty, `score` would skip the check.
    mentions_any: list[str] = Field([], min_length=1)
    mentions_all: list[str] = Field([], min_length=1)
    no_changes: bool = False
    superset: list[Superset] = []


class CaseFile(_Strict):
    """`case.yaml`. `id` and `category` are not fields: they come from the path."""

    starting_project: str | None = None
    # The agent whose policy the case edits: a name in the project's agents.json.
    agent: str = Field(min_length=1)
    request: str = Field(min_length=1)
    held_out: bool = False
    # Why the case exists, for whoever reads the results; never the agent.
    note: str = ""
    expect: Expect


_DECISIONS = TypeAdapter(list[Decision])


def _read_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise CaseError(f"{path}: {exc}") from exc


def starting_files(case: dict) -> dict[str, str]:
    """{relative path: text} of the starting project, as the agent receives it.

    `preserve.yaml` is ours, not the project's: it lists the calls the agent's
    edit must keep, so the agent never sees it. The paths are the ones
    `snapshot` compares, so no file passes `unchanged` without being compared.
    """
    root = case["project"]
    return {
        rel: (root / rel).read_text()
        for rel in sorted(snapshot(root))
        if rel != PRESERVE
    }


def _call(role: str, d: dict) -> str:
    # The whole call, so one intended change replaces only the preserved check
    # for that call, not every preserved check on the same tool.
    return dump_json(
        [
            role,
            d["tool"],
            d.get("args", {}),
            d.get("attributes", {}),
            d.get("run_facts", {}),
        ]
    )


def _roles(d: dict) -> list[str]:
    return d.get("roles") or [d["role"]]


def _merge_preserved(decisions: list[dict], preserved: list[dict]) -> list[dict]:
    """The case's decisions, then each preserved call the case doesn't redefine."""
    own = {_call(role, d) for d in decisions for role in _roles(d)}
    merged = list(decisions)
    for d in preserved:
        kept = [role for role in _roles(d) if _call(role, d) not in own]
        if kept == _roles(d):
            merged.append(d)
        elif kept:
            entry = {k: v for k, v in d.items() if k not in ("role", "roles")}
            merged.append({**entry, "roles": kept})
    return merged


def _project(root: Path, case_dir: Path, named: str | None) -> Path:
    own = case_dir / "starting_project"
    if named is not None and own.is_dir():
        raise CaseError(f"{case_dir}: both `starting_project:` and starting_project/")
    if own.is_dir():
        return own
    if named is None:
        raise CaseError(
            f"{case_dir}: neither `starting_project:` nor starting_project/"
        )
    shared = root / "starting_projects"
    if named not in {p.name for p in shared.glob("*") if p.is_dir()}:
        raise CaseError(f"{case_dir}: no starting project {named!r} in {shared}")
    return shared / named


def _calls(expect: dict) -> list[dict]:
    """Every dry-run call a case lists: its decisions and its superset probes."""
    probes = [p for s in expect.get("superset", []) for p in s["probes"]]
    return expect.get("decisions", []) + probes


def _complete_calls(path: Path, agent: str, calls: list[dict]) -> None:
    for call in calls:
        bad = complete(call, agent)
        if bad:
            raise CaseError(f"{path}: {call['tool']}: not what {agent} sends: {bad}")
        if call.get("run_facts"):
            try:
                check_run_facts(call, agent)
            except CaseError as exc:
                raise CaseError(f"{path}: {call['tool']}: {exc}") from exc


def _check_calls(path: Path, case: dict) -> None:
    """The case's agent is in agents.json, and its calls use only what it knows."""
    agent = case["agent"]
    try:
        known = load_known(case["project"], agent)
    # No agents.json, bad JSON, an unknown agent, a row or view of the wrong shape.
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CaseError(f"{path}: {exc}") from exc
    for call in _calls(case["expect"]):
        unknown = unknown_names(call, known)
        if unknown:
            raise CaseError(f"{path}: {call['tool']}: unknown to {agent}: {unknown}")
        bad = bad_values(call, known)
        if bad:
            raise CaseError(f"{path}: {call['tool']}: not {agent}'s schema: {bad}")


def _check_entries(case_dir: Path) -> None:
    # A misnamed folder (`wrong_answers/`) would never be scored.
    stray = sorted(
        p.name
        for p in case_dir.iterdir()
        if p.name not in CASE_ENTRIES and not p.name.startswith(".")
    )
    if stray:
        raise CaseError(f"{case_dir}: not part of a case: {stray}")


def _parse(path: Path) -> CaseFile:
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise CaseError(f"{path}: not a YAML mapping")
    shadowing = sorted({"id", "category"} & raw.keys())
    if shadowing:
        raise CaseError(f"{path}: {shadowing} come from the path, not the file")
    try:
        return CaseFile.model_validate(raw)
    except ValidationError as exc:
        raise CaseError(f"{path}: {exc}") from exc


def _protect(path: Path, expect: dict, files: dict[str, str]) -> None:
    """Add the protected starting files the case doesn't list to `unchanged`."""
    changed = set(expect.get("changed", []))
    unchanged = list(expect.get("unchanged", []))
    # A misspelt path is missing before and after, so its check always passes.
    missing = sorted(set(unchanged) - files.keys())
    if missing:
        raise CaseError(f"{path}: `unchanged` names no starting file: {missing}")
    for rel in files:
        protected = any(fnmatch.fnmatch(rel, pattern) for pattern in PROTECTED)
        if protected and rel not in changed and rel not in unchanged:
            unchanged.append(rel)
    if unchanged:
        expect["unchanged"] = unchanged


def _load_preserved(project: Path, files: dict[str, str], agent: str) -> list[dict]:
    """The project's `preserve.yaml` calls, completed like the case's own."""
    # A `preserve.yml` or `Preserve.yaml` would be skipped (or, on a
    # case-insensitive disk, read) and also handed to the agent as a project file.
    misnamed = sorted(
        rel for rel in files if "/" not in rel and Path(rel.lower()).stem == "preserve"
    )
    if misnamed:
        raise CaseError(f"{project}: {misnamed} should be {PRESERVE}")
    preserve = project / PRESERVE
    if not preserve.is_file():
        return []
    try:
        decisions = _DECISIONS.validate_python(_read_yaml(preserve) or [])
    except ValidationError as exc:
        raise CaseError(f"{preserve}: {exc}") from exc
    preserved = _DECISIONS.dump_python(decisions, exclude_unset=True)
    _complete_calls(preserve, agent, preserved)
    return preserved


def _load_case(root: Path, case_dir: Path) -> dict:
    path = case_dir / "case.yaml"
    _check_entries(case_dir)
    parsed = _parse(path)
    case = {
        "id": f"{case_dir.parent.name}/{case_dir.name}",
        "category": case_dir.parent.name,
        **parsed.model_dump(include={"agent", "request", "held_out", "note"}),
        "dir": case_dir,
        "project": _project(root, case_dir, parsed.starting_project),
        # As written, so `checks.score` reads only the keys the case sets.
        "expect": parsed.expect.model_dump(exclude_unset=True),
    }
    expect = case["expect"]
    files = starting_files(case)
    _protect(path, expect, files)
    # Before the merge, so a preserved call and the case's own entry for it
    # compare equal however much of the gate's arguments each spells out.
    _complete_calls(path, case["agent"], _calls(expect))
    preserved = _load_preserved(case["project"], files, case["agent"])
    if preserved:
        expect["decisions"] = _merge_preserved(expect.get("decisions", []), preserved)
    _check_calls(path, case)
    return case


def load_cases(root: Path = HERE) -> list[dict]:
    """Every `cases/<category>/<name>/case.yaml` under `root`, sorted by id.

    A case folder without `case.yaml` (a `case.yml`), or a `case.yaml` one
    level too deep or too shallow, is an error: skipped, its `solution/` would
    never be checked.
    """
    cases_dir = root / "cases"

    def tooling(p: Path) -> bool:
        # An editor's or notebook's dot folder, not a case.
        return any(part.startswith(".") for part in p.relative_to(cases_dir).parts)

    for path in cases_dir.rglob("case.yaml"):
        if path.parent.parent.parent != cases_dir and not tooling(path):
            raise CaseError(f"{path}: not at cases/<category>/<name>/case.yaml")
    cases = []
    for case_dir in sorted(cases_dir.glob("*/*")):
        if not case_dir.is_dir() or tooling(case_dir):
            continue
        if not (case_dir / "case.yaml").is_file():
            raise CaseError(f"{case_dir}: no case.yaml")
        cases.append(_load_case(root, case_dir))
    return cases


def apply_solution(case: dict, ws: Path, folder: str = "solution") -> str:
    """Copy one of our answers over the workspace; return its `ANSWER.md`.

    `folder` is `solution` (checks must accept it) or `wrong_answer` (checks
    must reject it). `ANSWER.md` is the final message we write in place of the
    agent's, so it is returned, not copied into the project.
    """
    src_root = case["dir"] / folder
    answer = ""
    for src in sorted(src_root.rglob("*")) if src_root.is_dir() else []:
        if not src.is_file():
            continue
        rel = src.relative_to(src_root)
        if rel.as_posix() == "ANSWER.md":
            answer = src.read_text()
            continue
        dst = ws / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return answer
