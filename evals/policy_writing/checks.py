"""Score a policy-writing workspace: what the agent (or one of our answers) left behind.

No eval framework is imported here: the Inspect task and the dataset tests both
call `score(case, workspace, before, answer)` and get back a list of `Check`s.
Each check runs the hexgate CLI in-process: the policy must validate without
lint warnings, every dry-run decision must match, files must change (or not) as
the case says, and the final answer must mention what the case requires.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from hexgate.cli import _build_parser


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


_CLI_LOCK = threading.Lock()


def hexgate(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `hexgate policy <args>` in-process (one interpreter start per run, not per call).

    stdout is process-global, so calls are serialised; scoring is cheap next to the agent.
    """
    out, err = io.StringIO(), io.StringIO()
    with _CLI_LOCK, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            ns = _build_parser().parse_args(["policy", *args])
            code = ns.func(ns) or 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return subprocess.CompletedProcess(
        ["hexgate", "policy", *args], code, out.getvalue(), err.getvalue()
    )


def snapshot(root: Path) -> dict[str, str]:
    """{relative path: sha256} of every file, skipping any path with a dot component.

    A dot directory is tooling, not project: inspect_swe installs the skill under
    `.claude/skills/`, and scoring writes `.effective.yaml`. Counting them would
    fail every `no_changes` case.
    """
    files = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() and not any(part.startswith(".") for part in rel.parts):
            files[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return files


def outcome_of(stdout: str) -> str:
    first = stdout.strip().splitlines()[0] if stdout.strip() else ""
    for word in ("APPROVAL_REQUIRED", "ALLOW", "DENY"):
        if word in first:
            return word.lower()
    return "error"


RANK = {"deny": 0, "approval_required": 1, "allow": 2}


def decide(policy: Path, role: str, d: dict) -> tuple[str, str]:
    proc = hexgate(
        "test",
        str(policy),
        "--role",
        role,
        "--tool",
        d["tool"],
        "--args",
        json.dumps(d.get("args", {})),
        "--attributes",
        json.dumps(d.get("attributes", {})),
        "--run-facts",
        json.dumps(d.get("run_facts", {})),
    )
    return outcome_of(proc.stdout), (proc.stdout + proc.stderr).strip()


# Arguments the synthetic keys carry (hexgate/security/network.py, the agent gate).
SYNTHETIC_ARGS = {
    "net.http_request": {"host", "scheme", "port", "path", "query"},
    "net.tcp_connect": {"host", "port", "protocol"},
}
AGENT_ARGS = {"agent", "target", "via"}
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


def policy_bodies(policy: Path) -> tuple[dict, list[dict]]:
    doc = yaml.safe_load(policy.read_text()) or {}
    bodies = [b for b in (doc.get("roles") or {}).values() if isinstance(b, dict)]
    return doc, bodies or [doc]


def policy_tools(policy: Path) -> set[str]:
    _, bodies = policy_bodies(policy)
    return {t for b in bodies for t in (b.get("tools") or {})}


def unknown_refs(
    policy: Path, tools: dict[str, set[str]], attrs: set[str]
) -> list[str]:
    """`args.x` / `ctx.x` a constraint uses that TOOLS.md doesn't define."""
    every_arg = set().union(*tools.values(), *SYNTHETIC_ARGS.values(), AGENT_ARGS)
    doc, bodies = policy_bodies(policy)
    # (tool or None for a policy- or role-level constraint, constraint text)
    lines = [(None, c) for c in doc.get("constraints") or []]
    for b in bodies:
        lines += [(None, c) for c in b.get("constraints") or []]
        for tool, spec in (b.get("tools") or {}).items():
            lines += [(tool, c) for c in (spec or {}).get("constraints") or []]
    bad = set()
    for tool, text in lines:
        for kind, name in REF.findall(str(text)):
            if kind == "ctx":
                ok = name in attrs
            elif tool is None:
                ok = name in every_arg
            elif tool.startswith("agent."):
                ok = name in AGENT_ARGS
            elif tool in SYNTHETIC_ARGS:
                ok = name in SYNTHETIC_ARGS[tool]
            else:
                ok = (
                    tool not in tools or name in tools[tool]
                )  # unknown tools fail elsewhere
            if not ok:
                bad.add(f"{tool or 'policy-level'}: {kind}.{name}")
    return sorted(bad)


def _failed(name: str, proc: subprocess.CompletedProcess[str]) -> Check:
    return Check(name, False, (proc.stdout + proc.stderr).strip()[-800:])


def effective_policy(ws: Path) -> tuple[Path | None, Check]:
    # `--max-severity warning`: a lint warning (e.g. a permissive default role)
    # fails `valid`, in either layout.
    if (ws / "policies").is_dir():
        check = hexgate("check", "--dir", str(ws), "--max-severity", "warning")
        if check.returncode != 0:
            return None, _failed("valid", check)
        out = ws / ".effective.yaml"
        res = hexgate("resolve", "--dir", str(ws), "-o", str(out))
        if res.returncode != 0:
            return None, _failed("valid", res)
        # `check` lints the modules (dead or erased grants) but not the roles
        # they compose into; a permissive `default` shows only on the result.
        val = hexgate("validate", str(out), "--max-severity", "warning")
        if val.returncode != 0:
            return None, _failed("valid", val)
        return out, Check("valid", True)
    policy = ws / "policy.yaml"
    val = hexgate("validate", str(policy), "--max-severity", "warning")
    if val.returncode != 0:
        return None, _failed("valid", val)
    return policy, Check("valid", True)


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
            if RANK.get(hi, -1) < RANK.get(lo, -1):
                worse.append(f"{p['tool']}: {s['narrower']}={lo}, {s['wider']}={hi}")
        checks.append(Check(name, not worse, "; ".join(worse)))

    if policy is not None:
        tools, attrs = read_manifest(ws)
        unknown = sorted(
            t
            for t in policy_tools(policy)
            if t not in tools and not t.startswith(("net.", "agent."))
        )
        checks.append(
            Check(
                "only known tools",
                not unknown,
                f"not in TOOLS.md: {unknown}" if unknown else "",
            )
        )
        refs = unknown_refs(policy, tools, attrs)
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
