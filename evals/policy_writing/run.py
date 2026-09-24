"""Run the policy-writing eval set against an agent and score what it wrote.

    # Check the dataset itself: the hand-written solutions must score 100%.
    uv run python evals/policy_writing/run.py --agent reference

    # Claude Code with the write-policy skill (needs the `claude` CLI, logged in).
    uv run python evals/policy_writing/run.py --agent claude --repeat 3

    # Any other agent: a shell command that edits $CASE_WORKSPACE and prints its
    # final answer on stdout. $CASE_PROMPT holds the full instruction.
    uv run python evals/policy_writing/run.py --agent-cmd "python my_agent.py"

Each case copies a fixture into a fresh workspace under .runs/, lets the agent
edit it, then scores the result with the hexgate CLI: the policy must validate,
every dry-run decision must match, files must change (or not) as the case says,
and the final answer must mention what the case requires. Results land in
.runs/<stamp>/results.json and report.md.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from hexgate.cli import _build_parser

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNS = HERE / ".runs"

# Answers the runner writes itself when the agent didn't finish (a throttled,
# crashed or timed-out session). Never inferred from the agent's own text: a
# correct answer may well talk about rate limits.
AGENT_ERROR = "<agent error>"
TIMED_OUT = "<timed out>"

# Exactly what a user would type. Everything the agent needs to know about the
# project comes from the workspace it starts in (TOOLS.md, the policy files)
# and from the skill, as it would in real use.
PROMPT = "/write-policy {request}"

# Facts about the harness, not the request: appended to the agent's system
# prompt, the way the platform will hand its own agent context.
HARNESS = (
    "You are running unattended in an automated evaluation, started in the "
    "project's directory. Nobody can answer questions, so don't ask any: make "
    "a reasonable assumption and state it. End with a short summary of what "
    "you changed."
)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Result:
    case: str
    category: str
    attempt: int
    workspace: str
    seconds: float
    answer: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


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
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    }


# ---------------------------------------------------------------- agents


def run_reference(case: dict, ws: Path, folder: str = "solutions") -> str:
    sol = HERE / folder / case["id"]
    answer = ""
    for src in sol.rglob("*") if sol.is_dir() else []:
        if not src.is_file():
            continue
        rel = src.relative_to(sol)
        if str(rel) == "ANSWER.md":
            answer = src.read_text()
            continue
        dst = ws / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return answer


def child_env() -> dict[str, str]:
    # A nested `claude` must not think it is running inside this session.
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))
    }


def run_claude(prompt: str, ws: Path, model: str | None, timeout: int) -> str:
    # Started inside the workspace, which sits in the repo, so the project's
    # skills load and `uv run hexgate` finds the project.
    cmd = [
        "claude",
        "-p",
        prompt,
        "--append-system-prompt",
        HARNESS,
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Glob,Grep,Edit,Write,Bash(uv run hexgate:*),Bash(hexgate:*)",
    ]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(
        cmd, cwd=ws, capture_output=True, text=True, timeout=timeout, env=child_env()
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return f"{AGENT_ERROR} {proc.stdout}{proc.stderr}"
    if out.get("is_error"):
        return f"{AGENT_ERROR} {out.get('result') or out.get('subtype', '')}"
    return out.get("result", "")


def run_cmd(command: str, case: dict, prompt: str, ws: Path, timeout: int) -> str:
    env = {
        **os.environ,
        "CASE_PROMPT": prompt,
        "CASE_SYSTEM": HARNESS,
        "CASE_REQUEST": case["request"],
        "CASE_WORKSPACE": str(ws),
    }
    proc = subprocess.run(
        command,
        shell=True,
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if proc.returncode != 0:
        return f"{AGENT_ERROR} exit {proc.returncode}: {proc.stderr or proc.stdout}"
    return proc.stdout


# ---------------------------------------------------------------- scoring


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


def effective_policy(ws: Path) -> tuple[Path | None, Check]:
    if (ws / "policies").is_dir():
        check = hexgate("check", "--dir", str(ws))
        if check.returncode != 0:
            return None, Check(
                "valid", False, (check.stdout + check.stderr).strip()[-800:]
            )
        out = ws / ".effective.yaml"
        res = hexgate("resolve", "--dir", str(ws), "-o", str(out))
        if res.returncode != 0:
            return None, Check("valid", False, (res.stdout + res.stderr).strip()[-800:])
        return out, Check("valid", True)
    policy = ws / "policy.yaml"
    val = hexgate("validate", str(policy))
    if val.returncode != 0:
        return None, Check("valid", False, (val.stdout + val.stderr).strip()[-800:])
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


# ---------------------------------------------------------------- driver


def run_case(case: dict, attempt: int, stamp: Path, args: argparse.Namespace) -> Result:
    ws = stamp / f"{case['id']}-{attempt}"
    shutil.copytree(HERE / "fixtures" / case["fixture"], ws)
    before = snapshot(ws)
    rel_ws = ws.relative_to(REPO)
    prompt = PROMPT.format(request=case["request"])
    start = time.monotonic()
    try:
        if args.agent_cmd:
            answer = run_cmd(args.agent_cmd, case, prompt, ws, args.timeout)
        elif args.agent == "reference":
            answer = run_reference(case, ws)
        elif args.agent == "negatives":
            answer = run_reference(case, ws, "negatives")
        else:
            answer = run_claude(prompt, ws, args.model, args.timeout)
    except subprocess.TimeoutExpired:
        answer = TIMED_OUT
    seconds = time.monotonic() - start
    (ws / ".answer.md").write_text(answer)
    checks = score(case, ws, before, answer)
    # A throttled, crashed or timed-out agent is an infrastructure failure, not
    # a bad policy: say so first, so it can't be mistaken for one.
    if answer.startswith((AGENT_ERROR, TIMED_OUT)):
        checks.insert(0, Check("agent ran", False, answer[:300]))
    return Result(
        case["id"],
        case.get("category", "other"),
        attempt,
        str(rel_ws),
        round(seconds, 1),
        answer,
        checks,
    )


def git_status() -> set[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True
    ).stdout
    return {line for line in out.splitlines() if ".runs" not in line}


def write_report(
    results: list[Result], stamp: Path, agent: str, stray: set[str]
) -> str:
    cases = list(dict.fromkeys(r.case for r in results))
    passed = sum(r.passed for r in results)
    lines = [
        f"# Policy-writing eval: {agent}",
        "",
        f"**{passed} / {len(results)} runs passed** ({100 * passed / max(len(results), 1):.0f}%) over {len(cases)} cases.",
        "",
        "| Category | Runs passed |",
        "|---|---|",
    ]
    for cat in dict.fromkeys(r.category for r in results):
        runs = [r for r in results if r.category == cat]
        lines.append(f"| {cat} | {sum(r.passed for r in runs)}/{len(runs)} |")
    lines += ["", "| Case | Category | Passed | Failed checks |", "|---|---|---|---|"]
    for cid in cases:
        runs = [r for r in results if r.case == cid]
        failed = sorted({c.name for r in runs for c in r.checks if not c.passed})
        lines.append(
            f"| `{cid}` | {runs[0].category} | {sum(r.passed for r in runs)}/{len(runs)} | {'; '.join(failed) or '-'} |"
        )
    if stray:
        lines += [
            "",
            "**Edits outside the workspace:**",
            *[f"- `{s}`" for s in sorted(stray)],
        ]
    report = "\n".join(lines) + "\n"
    (stamp / "report.md").write_text(report)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--agent",
        choices=("reference", "negatives", "claude"),
        default="reference",
        help="reference: the solutions (must all pass). negatives: known-bad answers (must all fail).",
    )
    ap.add_argument(
        "--agent-cmd", help="Shell command for any other agent (overrides --agent)."
    )
    ap.add_argument("--model", help="Model for --agent claude.")
    ap.add_argument(
        "--case",
        action="append",
        help="Run only cases whose id starts with this (repeatable).",
    )
    ap.add_argument(
        "--repeat", type=int, default=1, help="Attempts per case (LLM output varies)."
    )
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=900, help="Seconds per agent run.")
    args = ap.parse_args()

    cases = yaml.safe_load((HERE / "cases.yaml").read_text())
    if args.case:
        cases = [c for c in cases if any(c["id"].startswith(p) for p in args.case)]
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2

    agent = (
        f"cmd: {args.agent_cmd}"
        if args.agent_cmd
        else args.agent + (f" ({args.model})" if args.model else "")
    )
    stamp = RUNS / time.strftime("%Y%m%d-%H%M%S")
    stamp.mkdir(parents=True)
    before = git_status()
    jobs = [(c, n) for c in cases for n in range(1, args.repeat + 1)]
    with ThreadPoolExecutor(
        max_workers=1 if args.agent == "reference" and not args.agent_cmd else args.jobs
    ) as pool:
        results = list(pool.map(lambda j: run_case(j[0], j[1], stamp, args), jobs))
    stray = git_status() - before

    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"{mark}  {r.case}#{r.attempt}  ({r.seconds}s)")
        for c in r.checks:
            if not c.passed:
                print(f"      ✗ {c.name}  {c.detail}")
    (stamp / "results.json").write_text(
        json.dumps(
            [{**asdict(r), "passed": r.passed} for r in results],
            indent=2,
            ensure_ascii=False,
        )
    )
    print()
    print(write_report(results, stamp, agent, stray))
    print(f"workspaces and results: {stamp.relative_to(REPO)}")
    return 0 if all(r.passed for r in results) and not stray else 1


if __name__ == "__main__":
    sys.exit(main())
