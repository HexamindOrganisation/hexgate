"""The policy-writing eval set as an Inspect AI task (spike).

    uv run --with inspect-ai inspect eval evals/policy_writing/inspect_task.py \
        --model none -T agent=claude -T cases=f01,m03 --epochs 3 --max-samples 4
    uv run --with inspect-ai inspect view

Same cases, fixtures and scoring as run.py, which this imports: Inspect only
replaces the loop, the repeats and the viewer. The agent runs on the host, in a
workspace inside the repo, exactly as run.py runs it, so the project's skills
load and `uv run hexgate` resolves. Claude's stream-json output is replayed
into the sample's messages, so Inspect View shows every tool call it made.

-T agent: claude (default), reference (must all pass) or negatives (must all fail).
-T cases: comma-separated id prefixes; all cases when omitted.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageTool,
    ContentText,
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run  # noqa: E402  (evals/policy_writing is not a package)

WORKSPACES = run.RUNS / time.strftime("inspect-%Y%m%d-%H%M%S")


def load_samples(prefixes: list[str]) -> list[Sample]:
    cases = yaml.safe_load((HERE / "cases.yaml").read_text())
    if prefixes:
        cases = [c for c in cases if any(c["id"].startswith(p) for p in prefixes)]
    return [
        Sample(
            id=c["id"],
            input=run.PROMPT.format(request=c["request"]),
            metadata={"case": c, "category": c.get("category", "other")},
        )
        for c in cases
    ]


def _text(content) -> str:
    """A tool_result's content: a string, or a list of {type: text} blocks."""
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def run_claude_stream(prompt: str, ws: Path, timeout: int) -> tuple[str, list]:
    """Like run.run_claude, but keeps the transcript: (final answer, messages)."""
    cmd = [
        "claude",
        "-p",
        prompt,
        "--append-system-prompt",
        run.HARNESS,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Glob,Grep,Edit,Write,Bash(uv run hexgate:*),Bash(hexgate:*)",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run.child_env(),
        )
    except subprocess.TimeoutExpired:
        return run.TIMED_OUT, []
    messages: list = []
    names: dict[str, str] = {}
    answer = f"{run.AGENT_ERROR} no result event: {proc.stderr[-500:]}"
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        # Only assistant/user events carry a {content: [...]} message; others
        # (system, status) may hold a plain string there.
        msg = ev.get("message")
        blocks = (msg.get("content") if isinstance(msg, dict) else None) or []
        if kind == "assistant":
            text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
            calls = [
                ToolCall(id=b["id"], function=b["name"], arguments=b.get("input") or {})
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            names.update({c.id: c.function for c in calls})
            if text or calls:
                messages.append(
                    ChatMessageAssistant(content=text, tool_calls=calls or None)
                )
        elif kind == "user" and isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    messages.append(
                        ChatMessageTool(
                            content=_text(b.get("content")),
                            tool_call_id=b.get("tool_use_id"),
                            function=names.get(b.get("tool_use_id")),
                        )
                    )
        elif kind == "result":
            if ev.get("is_error"):
                answer = (
                    f"{run.AGENT_ERROR} {ev.get('result') or ev.get('subtype', '')}"
                )
            else:
                answer = ev.get("result", "")
    return answer, messages


@solver
def policy_agent(agent: str = "claude", timeout: int = 900):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        case = state.metadata["case"]
        ws = WORKSPACES / f"{case['id']}-{state.epoch}"
        shutil.copytree(HERE / "fixtures" / case["fixture"], ws)
        state.store.set("workspace", str(ws))
        state.store.set("before", run.snapshot(ws))
        if agent == "claude":
            answer, messages = await asyncio.to_thread(
                run_claude_stream, state.input_text, ws, timeout
            )
            state.messages.extend(messages)
        else:
            folder = "negatives" if agent == "negatives" else "solutions"
            answer = await asyncio.to_thread(run.run_reference, case, ws, folder)
        state.messages.append(ChatMessageAssistant(content=[ContentText(text=answer)]))
        state.store.set("answer", answer)
        return state

    return solve


@scorer(metrics=[accuracy(), stderr(), grouped(accuracy(), "category")])
def policy_checks():
    async def score(state: TaskState, target: Target) -> Score:
        ws = Path(state.store.get("workspace"))
        answer = state.store.get("answer", "")
        checks = await asyncio.to_thread(
            run.score, state.metadata["case"], ws, state.store.get("before"), answer
        )
        if answer.startswith((run.AGENT_ERROR, run.TIMED_OUT)):
            checks.insert(0, run.Check("agent ran", False, answer[:300]))
        failed = [c for c in checks if not c.passed]
        return Score(
            value=INCORRECT if failed else CORRECT,
            answer=answer[:2000],
            explanation="\n".join(
                f"{'✓' if c.passed else '✗'} {c.name}"
                + (f": {c.detail}" if c.detail else "")
                for c in checks
            ),
            metadata={"workspace": str(ws)},
        )

    return score


@task
def policy_writing(agent: str = "claude", cases: str = "", timeout: int = 900) -> Task:
    prefixes = [p.strip() for p in cases.split(",") if p.strip()]
    return Task(
        dataset=load_samples(prefixes),
        solver=policy_agent(agent, timeout),
        scorer=policy_checks(),
    )
