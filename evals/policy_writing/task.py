"""The policy-writing eval set as an Inspect AI task.

    uv run python -m inspect_ai eval evals/policy_writing/task.py --model none \\
        -T agent=reference --log-dir evals/policy_writing/logs
    uv run inspect view --log-dir evals/policy_writing/logs

Inspect gives each case a fresh sandbox holding only the starting project; the
scorer pulls the files left behind to the host and runs `checks.score` on them.
The local agents (`reference`: `solution/`, `wrong_answers`: `wrong_answer/`,
`noop`: nothing) test the checks and this wiring with no model and no Docker.
`claude` runs Claude Code itself (`claude -p`) in the Docker sandbox of
`compose.yaml` on the subscription token from `claude setup-token` (README): no
API key, still `--model none`.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    ChatMessageUser,
    ModelOutput,
)
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
from inspect_ai.tool import ToolCall
from inspect_ai.util import (
    OutputLimitExceededError,
    SandboxEnvironment,
    SandboxEnvironmentSpec,
    sample_limits,
    sandbox,
)

from evals.policy_writing import checks
from evals.policy_writing.cases import load_cases, starting_files

# Agent name → the answer folder its solver copies in. Only `wrong_answer/` is
# optional (README): without `solution/`, the answer is to do nothing.
OPTIONAL_FOLDER = "wrong_answer"
ANSWER_FOLDERS = {
    "reference": "solution",
    "wrong_answers": "wrong_answer",
    "noop": None,
}

# Claude Code in the sandbox: `claude -p` on the subscription token from `claude
# setup-token`, which compose.yaml takes from the host's environment.
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY_VARS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
COMPOSE = Path(__file__).with_name("compose.yaml")
REPO = Path(__file__).resolve().parents[2]
SKILL = Path(
    ".claude/skills/write-policy"
)  # a dot path: the snapshot and the pull skip it
# Every tool, unprompted, in the disposable container (non-root, which bypass
# requires), except the web: the agent reads only our files. Never `--bare`: it
# ignores the token.
CLAUDE_FLAGS = [
    *"--output-format stream-json --verbose --permission-mode bypassPermissions".split(),
    *["--disallowedTools", "WebFetch,WebSearch"],
]
# No eval-only system prompt: the agent gets the request and the skill, as in
# real use (design doc, Annex D).
NO_RESULT = "no result event"
USAGE_KEYS = ("usage", "total_cost_usd", "modelUsage", "duration_ms", "num_turns")
# Claude Code's session log: the only place the expanded slash command is written.
# Follows the Dockerfile's user, compose's working_dir and the pinned CLI's layout.
SESSION_LOG = "/home/agent/.claude/projects/-workspace/{}.jsonl"
# The agent's own timeout fires first, so no workspace in flux is ever scored.
SAMPLE_MARGIN = 60

# Bounds on what the scorer pulls back from the sandbox and puts in the log.
MAX_FILES = 200
MAX_BYTES = 1 << 20
MAX_DIFF_LINES = 200
MAX_ANSWER_CHARS = 4_000

# Each file's size in bytes, skipping dot paths (tooling such as `.claude/skills/`,
# as `checks.snapshot` does); `wc` adds a total line. Run as argv, not a shell.
LIST = "find . -type f ! -path */.* -exec wc -c {} +"

logger = logging.getLogger(__name__)

# Looked up by sample id: a case holds `Path`s, so it can't ride in `Sample.metadata`.
CASES = {case["id"]: case for case in load_cases()}


@task
def policy_writing(
    agent: str = "claude",
    cases: str | list[str] | None = None,
    timeout: int = 900,
    sandbox: str | None = None,
) -> Task:
    """`agent`: `claude`, `reference`, `wrong_answers` or `noop`. `cases`: id
    prefixes, all cases when empty. `timeout`: seconds the agent gets. `sandbox`:
    `claude` runs in `docker` and refuses any other; the rest default to `local`."""
    if agent == "claude":
        folder, solve, box = None, claude_agent(timeout), claude_sandbox(sandbox)
    elif agent in ANSWER_FOLDERS:
        folder = ANSWER_FOLDERS[agent]
        solve, box = our_answer(folder), sandbox or "local"
    else:
        raise ValueError(f"unknown agent {agent!r}")
    return Task(
        dataset=samples(folder, prefixes(cases)),
        solver=solve,
        scorer=policy_checks(),
        sandbox=box,
        time_limit=timeout + SAMPLE_MARGIN,
        # An infrastructure failure errors its sample, which leaves the metrics
        # and is retried by `inspect eval-retry`; the other samples carry on.
        fail_on_error=False if agent == "claude" else None,
    )


def claude_sandbox(kind: str | None) -> SandboxEnvironmentSpec:
    if kind not in (None, "docker"):
        raise ValueError(
            f"agent=claude runs only in the docker sandbox, not {kind!r}: it "
            "runs Claude Code with edits auto-accepted"
        )
    if not os.environ.get(TOKEN_VAR):
        raise RuntimeError(
            f"{TOKEN_VAR} is not set: run `claude setup-token` once and export "
            "the token it prints (README, 'Run Claude Code')"
        )
    return SandboxEnvironmentSpec("docker", str(COMPOSE))


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
    """Write the answer into the sandbox as `apply_solution` does; return `ANSWER.md`."""
    root, answer = case["dir"] / folder, ""
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel == "ANSWER.md":
            answer = path.read_text()
        else:
            await sb.write_file(rel, path.read_bytes())
    return answer


@solver
def claude_agent(timeout: int):
    """Claude Code with the write-policy skill, run inside the sandbox."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sb = sandbox()
        await check_sandbox_env(sb)
        await push_skill(sb)
        stdout, failure = await run_claude(sb, state.input_text, timeout)
        result, transcript = parse_stream(stdout)
        if infra := infra_error(stdout, result, failure):
            raise RuntimeError(infra)
        state.messages.extend(await injected(sb, result) + transcript)
        state.output = model_output(str(state.model), result, failure)
        state.store.set(
            "usage", {k: result.get(k) for k in USAGE_KEYS} if result else {}
        )
        return state

    return solve


async def check_sandbox_env(sb: SandboxEnvironment) -> None:
    """`--sandbox local` would run Claude Code on the host; a key would replace the token."""
    names = set(
        (await sb.exec(["sh", "-c", "env | cut -d= -f1"])).stdout.split()
    )  # no values: logged
    if "IS_SANDBOX" not in names:
        raise RuntimeError("not the compose.yaml sandbox: agent=claude runs only there")
    if found := API_KEY_VARS & names:
        raise RuntimeError(
            f"{', '.join(sorted(found))} set in the sandbox: Claude Code would "
            f"use it instead of {TOKEN_VAR} and bill it per token"
        )


async def push_skill(sb: SandboxEnvironment) -> None:
    root = REPO / SKILL
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        await sb.write_file(str(SKILL / path.relative_to(root)), path.read_bytes())


async def run_claude(
    sb: SandboxEnvironment, prompt: str, timeout: int
) -> tuple[str, str]:
    """Claude Code's stdout, and why if the run ended without a `result` event.
    Nothing of the host's env; no retry on a timeout, it would bill again."""
    cmd = ["claude", "-p", prompt, *CLAUDE_FLAGS]
    try:
        out = await sb.exec(cmd, timeout=timeout, timeout_retry=False)
    except (TimeoutError, OutputLimitExceededError) as exc:
        # Inspect keeps the output so far on it: None when nothing came out.
        return getattr(exc, "truncated_output", None) or "", str(exc)
    return out.stdout, f"{NO_RESULT}: {out.stderr[-500:]}"


def parse_stream(stdout: str) -> tuple[dict | None, list[ChatMessage]]:
    """The `result` event (None if the run ended without one) and the transcript
    replayed so Inspect View shows every tool call; other events are skipped."""
    result, messages = None, []
    for ev in events(stdout):
        kind, blocks = ev.get("type"), content_blocks(ev)
        if kind == "assistant":
            messages += assistant_message(blocks)
        elif kind == "user":
            messages += tool_messages(blocks)
        elif kind == "result":
            result = ev
    return result, messages


def events(stdout: str) -> Iterator[dict]:
    """The JSON object on each line, skipping stderr and a cut line (a timeout's output)."""
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def content_blocks(ev: dict) -> list[dict]:
    """A message's content blocks; a compaction summary holds a plain string instead."""
    msg = ev.get("message")
    blocks = msg.get("content") if isinstance(msg, dict) else None
    return blocks if isinstance(blocks, list) else []


def assistant_message(blocks: list[dict]) -> list[ChatMessage]:
    text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
    calls = [
        ToolCall(id=b["id"], function=b["name"], arguments=b.get("input") or {})
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    if not (text or calls):
        return []
    return [ChatMessageAssistant(content=text, tool_calls=calls or None)]


def tool_messages(blocks: list[dict]) -> list[ChatMessage]:
    return [
        ChatMessageTool(
            content=tool_text(b.get("content")), tool_call_id=b["tool_use_id"]
        )
        for b in blocks
        if b.get("type") == "tool_result"
    ]


def tool_text(content) -> str:  # a string, or a list of {type: text} blocks
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


async def injected(sb: SandboxEnvironment, result: dict | None) -> list[ChatMessage]:
    """What Claude Code injected for the slash command (the skill), as user turns."""
    if not (result and result.get("session_id")):
        return []
    try:
        log = await sb.read_file(SESSION_LOG.format(result["session_id"]))
    except OSError as exc:  # the path drifted from the image or the CLI
        logger.warning("no Claude Code session log, skill turn not shown: %s", exc)
        return []
    metas = [ev for ev in events(log) if ev.get("type") == "user" and ev.get("isMeta")]
    blocks = [b for ev in metas for b in content_blocks(ev) if b.get("type") == "text"]
    return [ChatMessageUser(content=b["text"]) for b in blocks]


def infra_error(stdout: str, result: dict | None, failure: str) -> str | None:
    """Why the run failed for a reason that is not the agent's: an API error
    (bad token, rate limit) or a CLI that never started. Timeouts, max turns
    and errors during execution are the agent's, scored by `agent ran`."""
    if result and (
        result.get("api_error_status") or result.get("terminal_reason") == "api_error"
    ):
        return (
            f"Claude API error {result.get('api_error_status')}: {result.get('result')}"
        )
    if result is None and failure.startswith(NO_RESULT) and not any(events(stdout)):
        return f"Claude Code never started: {failure}"
    return None


def model_output(model: str, result: dict | None, failure: str) -> ModelOutput:
    if result is None:
        return ModelOutput.from_content(model, "", error=failure)
    if result.get("is_error"):
        error = result.get("result") or result.get("subtype") or "is_error"
        return ModelOutput.from_content(model, "", error=str(error))
    return ModelOutput.from_content(model, result.get("result") or "")


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
            size, pulled = await pull(sandbox(), Path(tmp))
            case, answer = CASES[state.sample_id], state.output.completion
            score = grade(case, Path(tmp), answer, size, agent_ran(state) or pulled)
        # What Claude Code reported, for the cost report (PR 15).
        score.metadata["usage"] = state.store.get("usage")
        return score

    return score


async def pull(
    sb: SandboxEnvironment, dest: Path
) -> tuple[tuple[int, int], checks.Check | None]:
    """A read that fails is the agent's doing (it outlived its limit): `agent ran` fails."""
    try:
        return await pull_workspace(sb, dest), None
    except (OSError, OutputLimitExceededError) as exc:
        detail = f"reading the sandbox failed: {exc}"
        return (0, 0), checks.Check("agent ran", False, detail)


async def pull_workspace(sb: SandboxEnvironment, dest: Path) -> tuple[int, int]:
    """Measure the sandbox's files, copy them to `dest` if within the bounds: (files, bytes)."""
    listing = await sb.exec(LIST.split())
    if not listing.success:
        raise RuntimeError(f"listing the sandbox failed: {listing.stderr}")
    sizes = {}
    for line in listing.stdout.splitlines():
        count, _, path = line.strip().partition(" ")
        if path.lstrip().startswith("./"):
            sizes[path.lstrip().removeprefix("./")] = int(count)
    files, total = len(sizes), sum(sizes.values())
    if files <= MAX_FILES and total <= MAX_BYTES:
        for rel in sizes:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_bytes(await sb.read_file(rel, text=False))
    return files, total


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


def grade(case: dict, ws: Path, answer: str, size: tuple[int, int], ran=None) -> Score:
    """Score a workspace pulled to the host, `size` being the sandbox's (files, bytes).
    `before` is the starting project hashed on the host, never the sandbox's files."""
    start = starting_files(case)
    before = hashes(start)
    results = [ran] if ran else []
    changes: dict[str, str] | None = None
    files, total = size
    if files > MAX_FILES or total > MAX_BYTES:
        detail = f"{files} files, {total} bytes; at most {MAX_FILES} and {MAX_BYTES}"
        results.append(checks.Check("workspace size", False, detail))
    else:
        after = checks.snapshot(ws)
        results += checks.score(case, ws, before, answer)
        changes = diffs(start, before, after, ws)
    return Score(
        value=CORRECT if all(c.passed for c in results) else INCORRECT,
        answer=answer,
        explanation=explanation(results, answer, changes),
        metadata={"checks": [asdict(c) for c in results]},
    )


def hashes(files: dict[str, str]) -> dict[str, str]:
    """`checks.snapshot` of these files, from the UTF-8 bytes Inspect sends to the sandbox."""
    return {
        rel: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for rel, text in files.items()
    }


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
