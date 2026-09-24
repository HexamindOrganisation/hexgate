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

PROMPT = """/write-policy {request}

The policy lives in `{workspace}`, a {layout}. The agent's tools, their \
arguments, the roles and the caller attributes are listed in \
`{workspace}/TOOLS.md`. Edit only files under `{workspace}`.

This is a non-interactive run: do not ask questions. Make a reasonable \
assumption and state it. If part of the request can't be expressed in a \
policy, say so plainly. End with a short summary of what you changed."""

LAYOUTS = {
    "flat": "single role-keyed file, policy.yaml",
    "agents": "single role-keyed file, policy.yaml",
    "modules": "policy module tree (policies/boundaries, policies/capabilities, roles.yaml)",
}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Result:
    case: str
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


def run_reference(case: dict, ws: Path) -> str:
    sol = HERE / "solutions" / case["id"]
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


def run_claude(prompt: str, model: str | None, timeout: int) -> str:
    cmd = [
        "claude",
        "-p",
        prompt,
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
        cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout, env=child_env()
    )
    try:
        return json.loads(proc.stdout).get("result", "")
    except json.JSONDecodeError:
        return proc.stdout + proc.stderr


def run_cmd(command: str, case: dict, prompt: str, ws: Path, timeout: int) -> str:
    env = {
        **os.environ,
        "CASE_PROMPT": prompt,
        "CASE_REQUEST": case["request"],
        "CASE_WORKSPACE": str(ws),
    }
    proc = subprocess.run(
        command,
        shell=True,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return proc.stdout


# ---------------------------------------------------------------- scoring


def outcome_of(stdout: str) -> str:
    first = stdout.strip().splitlines()[0] if stdout.strip() else ""
    for word in ("APPROVAL_REQUIRED", "ALLOW", "DENY"):
        if word in first:
            return word.lower()
    return "error"


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
        label = f"{d['role']} → {d['tool']}({json.dumps(d.get('args', {}), sort_keys=True)})"
        if d.get("attributes"):
            label += f" ctx={json.dumps(d['attributes'], sort_keys=True)}"
        if d.get("run_facts"):
            label += f" run={json.dumps(d['run_facts'], sort_keys=True)}"
        if policy is None:
            checks.append(Check(f"decision: {label}", False, "policy invalid"))
            continue
        proc = hexgate(
            "test",
            str(policy),
            "--role",
            d["role"],
            "--tool",
            d["tool"],
            "--args",
            json.dumps(d.get("args", {})),
            "--attributes",
            json.dumps(d.get("attributes", {})),
            "--run-facts",
            json.dumps(d.get("run_facts", {})),
        )
        got = outcome_of(proc.stdout)
        detail = (
            ""
            if got == d["expect"]
            else f"expected {d['expect']}, got {got}: {(proc.stdout + proc.stderr).strip()[:300]}"
        )
        checks.append(Check(f"decision: {label}", got == d["expect"], detail))

    after = snapshot(ws)
    for rel in expect.get("changed", []):
        checks.append(Check(f"changed: {rel}", after.get(rel) != before.get(rel)))
    for rel in expect.get("unchanged", []):
        checks.append(Check(f"unchanged: {rel}", after.get(rel) == before.get(rel)))

    words = expect.get("mentions_any")
    if words:
        hit = [w for w in words if w.lower() in answer.lower()]
        checks.append(
            Check(
                "answer mentions",
                bool(hit),
                f"found {hit}" if hit else f"none of {words}",
            )
        )
    return checks


# ---------------------------------------------------------------- driver


def run_case(case: dict, attempt: int, stamp: Path, args: argparse.Namespace) -> Result:
    ws = stamp / f"{case['id']}-{attempt}"
    shutil.copytree(HERE / "fixtures" / case["fixture"], ws)
    before = snapshot(ws)
    rel_ws = ws.relative_to(REPO)
    prompt = PROMPT.format(
        request=case["request"], workspace=rel_ws, layout=LAYOUTS[case["fixture"]]
    )
    start = time.monotonic()
    try:
        if args.agent_cmd:
            answer = run_cmd(args.agent_cmd, case, prompt, ws, args.timeout)
        elif args.agent == "reference":
            answer = run_reference(case, ws)
        else:
            answer = run_claude(prompt, args.model, args.timeout)
    except subprocess.TimeoutExpired:
        answer = "<timed out>"
    seconds = time.monotonic() - start
    (ws / ".answer.md").write_text(answer)
    return Result(
        case["id"],
        attempt,
        str(rel_ws),
        round(seconds, 1),
        answer,
        score(case, ws, before, answer),
    )


def git_status() -> set[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True
    ).stdout
    return {line for line in out.splitlines() if ".runs" not in line}


def write_report(
    results: list[Result], stamp: Path, agent: str, stray: set[str]
) -> str:
    cases = sorted({r.case for r in results})
    passed = sum(r.passed for r in results)
    lines = [
        f"# Policy-writing eval: {agent}",
        "",
        f"**{passed} / {len(results)} runs passed** ({100 * passed / max(len(results), 1):.0f}%) over {len(cases)} cases.",
        "",
        "| Case | Passed | Failed checks |",
        "|---|---|---|",
    ]
    for cid in cases:
        runs = [r for r in results if r.case == cid]
        failed = sorted({c.name for r in runs for c in r.checks if not c.passed})
        lines.append(
            f"| `{cid}` | {sum(r.passed for r in runs)}/{len(runs)} | {'; '.join(failed) or '-'} |"
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
    ap.add_argument("--agent", choices=("reference", "claude"), default="reference")
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
