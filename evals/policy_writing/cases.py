"""Load the policy-writing cases: one `case.yaml` per directory, grouped by category.

No eval framework is imported here. `load_cases` turns each case into a dict
that `checks.score` reads, with the checks every case shares already added:
the protected files must stay unchanged, and the starting project's
`preserve.yaml` calls must keep their result. `apply_solution` puts one of our
answers (`solution/` or `wrong_answer/`) into a workspace in place of an agent.
"""

from __future__ import annotations

import fnmatch
import json
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

from evals.policy_writing.checks import (
    AGENT_REACH_ARGS,
    SYNTHETIC_ARGS,
    load_known_names,
    snapshot,
)
from hexgate.egress.model import connect_to_args, http_to_args
from hexgate.manifest.models import AgentManifest, InputSchema
from hexgate.security.models import AGENT_REACH_PREFIXES, AGENT_RUN_TOOL
from hexgate.security.network import NET_HTTP_REQUEST, NET_TCP_CONNECT

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


class CaseError(ValueError):
    """A case directory that would load into a case scoring the wrong thing."""


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
    return json.dumps(
        [
            role,
            d["tool"],
            d.get("args", {}),
            d.get("attributes", {}),
            d.get("run_facts", {}),
        ],
        sort_keys=True,
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


def _unknown_names(
    call: dict, tools: dict[str, set[str]], attrs: set[str], agents: set[str]
) -> list:
    """Tool, argument and attribute names a dry-run call uses that the agent lacks.

    The SDK denies an unknown tool, a reach to an agent no rule names, and a
    constraint on an argument the call doesn't carry, so a misspelt name would
    pass every `deny` check.
    """
    tool = call["tool"]
    if tool in SYNTHETIC_ARGS:
        known = SYNTHETIC_ARGS[tool]
    elif tool.startswith(AGENT_REACH_PREFIXES):  # agent.<via>:<target>
        if tool.split(":", 1)[1] not in agents:
            return [tool]
        known = AGENT_REACH_ARGS
    elif tool in tools:
        known = tools[tool]
    else:
        return [tool]
    bad = [f"args.{a}" for a in call.get("args", {}) if a not in known]
    return bad + [f"ctx.{a}" for a in call.get("attributes", {}) if a not in attrs]


# JSON-schema types an argument value must have; bool is not a number in JSON.
_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _bad_args(call: dict, schema: InputSchema) -> list[str]:
    """Required arguments a manifest tool's call leaves out, and mistyped values.

    The SDK denies a call whose constraint reads an argument it doesn't carry
    or compares a value of the wrong type, so either would pass every `deny`
    check. YAML reads a quoted `"51"` as a string and a blank `amount:` as null.
    """
    args = call.get("args", {})
    bad = [f"args.{a} missing" for a in schema.required if a not in args]
    for name, value in args.items():
        kind = schema.properties[name].type
        # Adapters record "string" for any schema without one top-level type
        # (`int | None`, `bool | None`, a list, a model), so it says nothing; a
        # precise type rules out null too, since a nullable one is never precise.
        want = None if kind == "string" else _TYPES.get(kind)
        if want and not _is_a(value, want):
            bad.append(f"args.{name}={value!r} is not {kind}")
    return bad


def _is_a(value, want) -> bool:
    # bool is an int in Python, not a number in JSON.
    return isinstance(value, want) and (want is bool) == isinstance(value, bool)


def _json_type(value) -> str:
    for name, kind in _TYPES.items():
        if name != "integer" and _is_a(value, kind):
            return name
    return "null"


def _is_json(value) -> bool:
    # What a real call's arguments can hold; YAML also makes dates and times.
    if isinstance(value, list):
        return all(_is_json(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json(v) for k, v in value.items())
    return value is None or isinstance(value, (str, int, float, bool))


def _complete_synthetic(call: dict, agent: str) -> list[str]:
    """Fill the arguments the gates always send; what a call contradicts or lacks.

    `agent.run` carries the running agent, and `agent.<via>:<target>` also its
    target and via (hexgate/security/agent_gate.py); `net.tcp_connect` carries
    protocol "tcp" (hexgate/egress/tcp.py). Every `net.*` call carries an int
    port and the arguments below (hexgate/egress/model.py). A dry-run without
    them, or with other values, is denied by any constraint on them, so it
    would pass every `deny` check.
    """
    tool, args = call["tool"], call.setdefault("args", {})
    bad = [f"args.{k}={v!r} is not JSON" for k, v in args.items() if not _is_json(v)]
    if tool.startswith(AGENT_REACH_PREFIXES):
        via, target = tool.removeprefix("agent.").split(":", 1)
        sent, needed = {"agent": agent, "target": target, "via": via}, ()
    elif tool == AGENT_RUN_TOOL:
        sent, needed = {"agent": agent}, ()
    elif tool == NET_TCP_CONNECT:
        sent, needed = {"protocol": "tcp"}, ("host", "port")
    elif tool == NET_HTTP_REQUEST:
        # What the proxy builds from the request line, so the rest must agree.
        if args.get("method") == "CONNECT":
            needed = ("host", "port")
            ok = isinstance(args.get("host"), str) and _is_a(args.get("port"), int)
            sent = connect_to_args(args["host"], args["port"]) if ok else {}
        else:
            needed = ("method", "url")
            ok = all(isinstance(args.get(k), str) for k in needed)
            sent = http_to_args(args["method"], args["url"]) if ok else {}
    else:
        return bad
    bad += [f"args.{k} missing" for k in needed if args.get(k) is None]
    port = args.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
        bad.append(f"args.port={port!r} is not an int")
    bad += [
        f"args.{k}={args[k]!r}, sent {v!r}"
        for k, v in sent.items()
        if args.get(k, v) != v
    ]
    args.update(sent)
    return bad


def _complete_calls(path: Path, agent: str, calls: list[dict]) -> None:
    for call in calls:
        bad = _complete_synthetic(call, agent)
        if bad:
            raise CaseError(f"{path}: {call['tool']}: not what {agent} sends: {bad}")


def _check_names(path: Path, case: dict) -> None:
    """The case's agent is in agents.json, and its calls use only names it knows."""
    agent, expect = case["agent"], case["expect"]
    try:
        tools, attrs = load_known_names(case["project"], agent)
        views = json.loads((case["project"] / "agents.json").read_text())
        agents = {v["name"] for v in views}
        [view] = [v for v in views if v["name"] == agent]
        schemas = {
            t.name: t.input_schema
            for t in AgentManifest.model_validate(view["manifest"]).tools
        }
        audit = case["project"] / "audit.json"
        rows = json.loads(audit.read_text()) if audit.exists() else []
        rows = rows["rows"] if isinstance(rows, dict) else rows
        # The JSON types each attribute arrived with: a quoted "2" for an int
        # attribute is denied by `ctx.clearance >= 2` whatever the threshold.
        seen: dict[str, set[str]] = {}
        for row in rows:
            if row["agent_name"] == agent:
                for k, v in (row.get("attributes") or {}).items():
                    seen.setdefault(k, set()).add(_json_type(v))
    # No agents.json, bad JSON, an unknown agent, a row or view of the wrong shape.
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CaseError(f"{path}: {exc}") from exc
    calls = expect.get("decisions", []) + [
        p for s in expect.get("superset", []) for p in s["probes"]
    ]
    for call in calls:
        unknown = _unknown_names(call, tools, attrs, agents)
        if unknown:
            raise CaseError(f"{path}: {call['tool']}: unknown to {agent}: {unknown}")
        bad = _bad_args(call, schemas[call["tool"]]) if call["tool"] in schemas else []
        bad += [
            f"ctx.{k}={v!r} is not {' or '.join(sorted(seen[k]))}"
            for k, v in call.get("attributes", {}).items()
            if _json_type(v) not in seen[k]
        ]
        if bad:
            raise CaseError(f"{path}: {call['tool']}: not {agent}'s schema: {bad}")


def _load_case(root: Path, case_dir: Path) -> dict:
    path = case_dir / "case.yaml"
    # A misnamed folder (`wrong_answers/`) would never be scored.
    stray = sorted(
        p.name
        for p in case_dir.iterdir()
        if p.name not in CASE_ENTRIES and not p.name.startswith(".")
    )
    if stray:
        raise CaseError(f"{case_dir}: not part of a case: {stray}")
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise CaseError(f"{path}: not a YAML mapping")
    shadowing = sorted({"id", "category"} & raw.keys())
    if shadowing:
        raise CaseError(f"{path}: {shadowing} come from the path, not the file")
    try:
        parsed = CaseFile.model_validate(raw)
    except ValidationError as exc:
        raise CaseError(f"{path}: {exc}") from exc

    case = {
        "id": f"{case_dir.parent.name}/{case_dir.name}",
        "category": case_dir.parent.name,
        "agent": parsed.agent,
        "request": parsed.request,
        "held_out": parsed.held_out,
        "note": parsed.note,
        "dir": case_dir,
        "project": _project(root, case_dir, parsed.starting_project),
        # As written, so `checks.score` reads only the keys the case sets.
        "expect": parsed.expect.model_dump(exclude_unset=True),
    }
    expect = case["expect"]

    files = starting_files(case)
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

    # A `preserve.yml` or `Preserve.yaml` would be skipped (or, on a
    # case-insensitive disk, read) and also handed to the agent as a project file.
    misnamed = sorted(
        rel for rel in files if "/" not in rel and Path(rel.lower()).stem == "preserve"
    )
    if misnamed:
        raise CaseError(f"{case['project']}: {misnamed} should be {PRESERVE}")
    # Before the merge, so a preserved call and the case's own entry for it
    # compare equal however much of the gate's arguments each spells out.
    _complete_calls(
        path,
        case["agent"],
        expect.get("decisions", [])
        + [p for s in expect.get("superset", []) for p in s["probes"]],
    )
    preserve = case["project"] / PRESERVE
    if preserve.is_file():
        try:
            decisions = _DECISIONS.validate_python(_read_yaml(preserve) or [])
        except ValidationError as exc:
            raise CaseError(f"{preserve}: {exc}") from exc
        preserved = _DECISIONS.dump_python(decisions, exclude_unset=True)
        _complete_calls(preserve, case["agent"], preserved)
        expect["decisions"] = _merge_preserved(expect.get("decisions", []), preserved)
    _check_names(path, case)
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
