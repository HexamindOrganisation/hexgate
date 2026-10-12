"""The policy-writing eval set as an Inspect AI task.

    uv run python -m inspect_ai eval evals/policy_writing/task.py --model none \\
        -T agent=reference --log-dir evals/policy_writing/logs
    uv run inspect view --log-dir evals/policy_writing/logs

Inspect gives each case a fresh sandbox holding only the starting project; the
scorer pulls the files left behind to the host and runs `checks.score` on them.
The local agents (`reference`: `solution/`, `wrong_answers`: `wrong_answer/`,
`noop`: nothing) test the checks and this wiring with no model and no Docker.
Every sandbox read and write goes through `sandbox()`, so the Claude Code agent
(PR 5) can switch it to Docker without touching this file.
"""

from __future__ import annotations

import difflib
import tempfile
from dataclasses import asdict
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Target,
    accuracy,
    grouped,
    scorer,
    stderr,
)
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import SandboxEnvironment, sample_limits, sandbox

from evals.policy_writing import checks
from evals.policy_writing.cases import apply_solution, load_cases, starting_files

# Agent name → the answer folder its solver copies in. Only `wrong_answer/` is
# optional (README): without `solution/`, the answer is to do nothing.
OPTIONAL_FOLDER = "wrong_answer"
ANSWER_FOLDERS = {
    "reference": "solution",
    "wrong_answers": "wrong_answer",
    "noop": None,
}

# Bounds on what the scorer pulls back from the sandbox and puts in the log.
MAX_FILES = 200
MAX_BYTES = 1 << 20
MAX_DIFF_LINES = 200
MAX_ANSWER_CHARS = 4_000

# Looked up by sample id: a case holds `Path`s, so it can't ride in `Sample.metadata`.
CASES = {case["id"]: case for case in load_cases()}


@task
def policy_writing(
    agent: str = "claude", cases: str | list[str] | None = None, timeout: int = 900
) -> Task:
    """`agent`: `claude`, `reference`, `wrong_answers` or `noop`. `cases`: id
    prefixes, all cases when empty. `timeout`: seconds per sample."""
    if agent == "claude":
        raise NotImplementedError(
            "agent=claude (Claude Code in a Docker sandbox) lands in PR 5; "
            "run reference, wrong_answers or noop"
        )
    if agent not in ANSWER_FOLDERS:
        raise ValueError(
            f"unknown agent {agent!r}; one of claude, {', '.join(ANSWER_FOLDERS)}"
        )
    folder = ANSWER_FOLDERS[agent]
    return Task(
        dataset=samples(folder, prefixes(cases)),
        solver=our_answer(folder),
        scorer=policy_checks(),
        sandbox="local",
        time_limit=timeout,
    )


def prefixes(cases: str | list[str] | None) -> list[str]:
    """Inspect passes `-T cases=a,b` as a list and `-T cases=a` as a string."""
    items = cases.split(",") if isinstance(cases, str) else cases or []
    return [p.strip() for p in items if p.strip()]


def samples(folder: str | None, wanted: list[str]) -> list[Sample]:
    """One sample per case (that has `folder`, when optional): request + starting project."""
    return [
        Sample(
            id=case["id"],
            input=f"/write-policy {case['request']}",
            files=starting_files(case),
            metadata={
                "case_id": case["id"],
                "category": case["category"],
                "held_out": case["held_out"],
            },
        )
        for case in CASES.values()
        if (not wanted or case["id"].startswith(tuple(wanted)))
        and (folder != OPTIONAL_FOLDER or (case["dir"] / folder).is_dir())
    ]


@solver
def our_answer(folder: str | None):
    """Put one of our answers (or nothing) in the sandbox in place of an agent."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        answer = ""
        if folder:
            answer = await push_answer(sandbox(), CASES[state.sample_id], folder)
        state.output = ModelOutput.from_content(str(state.model), answer)
        return state

    return solve


async def push_answer(sb: SandboxEnvironment, case: dict, folder: str) -> str:
    """Copy the answer's files into the sandbox; return its `ANSWER.md`."""
    with tempfile.TemporaryDirectory() as tmp:
        answer = apply_solution(case, Path(tmp), folder)
        for rel in checks.snapshot(Path(tmp)):
            await sb.write_file(rel, (Path(tmp) / rel).read_bytes())
    return answer


@scorer(
    metrics=[
        accuracy(),
        stderr(),
        grouped(accuracy(), "category", all=False),  # "all" would repeat accuracy
        grouped(
            accuracy(), "held_out", all=False, name_template="held_out_{group_name}"
        ),
    ]
)
def policy_checks():
    """Score the files the agent left in the sandbox, on a host copy."""

    async def score(state: TaskState, target: Target) -> Score:
        with tempfile.TemporaryDirectory() as tmp:
            await pull_workspace(sandbox(), Path(tmp))
            case, answer = CASES[state.sample_id], state.output.completion
            return grade(case, Path(tmp), answer, agent_ran(state))

    return score


async def pull_workspace(sb: SandboxEnvironment, dest: Path) -> None:
    """Copy the sandbox's files to `dest`, stopping one file or one read past
    the bounds: `grade` measures the copy and judges it."""
    # No dot path: tooling (`.claude/skills/`), not project, as `checks.snapshot` has it.
    listing = await sb.exec(["find", ".", "-type", "f", "!", "-path", "*/.*"])
    if not listing.success:
        raise RuntimeError(f"listing the sandbox failed: {listing.stderr}")
    paths = [p.removeprefix("./") for p in listing.stdout.splitlines()]
    total = 0
    for rel in paths[: MAX_FILES + 1]:
        data = await sb.read_file(rel, text=False)
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(data)
        total += len(data)
        if total > MAX_BYTES:
            break


def agent_ran(state: TaskState) -> checks.Check | None:
    """A failing check when the agent didn't finish: a model error or a sample limit."""
    if state.output.error:
        return checks.Check(
            "agent ran", False, state.output.error[: checks.DETAIL_MAX_CHARS]
        )
    hit = [
        kind
        for kind, limit in vars(sample_limits()).items()
        if limit.remaining is not None and limit.remaining <= 0
    ]
    if hit:
        return checks.Check("agent ran", False, f"{', '.join(hit)} limit reached")
    return None


def grade(case: dict, ws: Path, answer: str, ran: checks.Check | None = None) -> Score:
    """Score a workspace pulled to the host. `before` is the starting project as
    the sandbox received it, written on the host: never the sandbox's own files."""
    start = starting_files(case)
    with tempfile.TemporaryDirectory() as tmp:
        write_files(Path(tmp), start)
        before = checks.snapshot(Path(tmp))
    after = checks.snapshot(ws)
    size = sum((ws / rel).stat().st_size for rel in after)
    results = [ran] if ran else []
    changes: dict[str, str] | None = None
    if len(after) > MAX_FILES or size > MAX_BYTES:
        # Not diffed either: the one read past the byte bound may be huge.
        detail = (
            f"{len(after)} files, {size} bytes; at most {MAX_FILES} and {MAX_BYTES}"
        )
        results.append(checks.Check("workspace size", False, detail))
    else:
        results += checks.score(case, ws, before, answer)
        changes = diffs(start, before, after, ws)
    return Score(
        value=CORRECT if all(c.passed for c in results) else INCORRECT,
        answer=answer,
        explanation=explanation(results, answer, changes),
        metadata={"checks": [asdict(c) for c in results]},
    )


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        # The bytes Inspect sends to the sandbox, whatever the host locale.
        (root / rel).write_text(text, encoding="utf-8")


def diffs(
    start: dict[str, str], before: dict[str, str], after: dict[str, str], ws: Path
) -> dict[str, str]:
    """{path: unified diff} of every added, changed or removed file, cut at MAX_DIFF_LINES."""
    out = {}
    for rel in sorted(before.keys() | after.keys()):
        if before.get(rel) == after.get(rel):
            continue
        old = start.get(rel, "").splitlines()
        new = (
            (ws / rel).read_text("utf-8", "replace").splitlines()
            if rel in after
            else []
        )
        lines = list(
            difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}", lineterm="")
        )
        if len(lines) > MAX_DIFF_LINES:
            lines[MAX_DIFF_LINES:] = [f"… {len(lines) - MAX_DIFF_LINES} more lines"]
        out[rel] = "\n".join(lines)
    return out


def explanation(
    results: list[checks.Check], answer: str, changes: dict[str, str] | None
) -> str:
    """Markdown for Inspect View: checks, answer, diffs (`changes` is None past the bounds)."""
    marks = [
        f"- {'✓' if c.passed else '✗'} {c.name}"
        + (": " + c.detail.replace("\n", "\n  ") if c.detail else "")
        for c in results
    ]
    shown = answer[:MAX_ANSWER_CHARS] + ("…" if len(answer) > MAX_ANSWER_CHARS else "")
    if changes is None:
        sections = ["(not diffed: workspace past the bounds)"]
    else:
        sections = [f"### {rel}\n```diff\n{diff}\n```" for rel, diff in changes.items()]
    head = ["## Checks", *marks, "", "## Answer", shown or "(none)", "", "## Changes"]
    return "\n".join(head + (sections or ["(no file changed)"]))
