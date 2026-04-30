"""Workspace-backed shell execution tool."""

from __future__ import annotations

from fortify.runtime import ToolUseContext
from fortify.tools.decorators import agent_tool
from fortify.tools.files._common import require_workspace


def _truncate_command(command: str, *, limit: int = 48) -> str:
    """Return a compact one-line preview of a shell command."""
    compact = " ".join(command.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


def _format_bash_call(arguments: dict[str, object]) -> str:
    """Format a compact label for a bash invocation."""
    command = arguments.get("command")
    if isinstance(command, str) and command.strip():
        return f"running {_truncate_command(command.strip())}"
    return "running command"


@agent_tool(
    name="bash",
    call_formatter=_format_bash_call,
    failure_mode="result",
)
async def bash(
    command: str,
    timeout_seconds: int = 30,
    tool_use_context: ToolUseContext | None = None,
) -> dict[str, object]:
    """Run one shell command inside the active workspace."""
    if tool_use_context is None:
        raise RuntimeError("This tool requires an active tool_use_context.")
    workspace = require_workspace(tool_use_context)
    result = await workspace.run_command(command, timeout_seconds=timeout_seconds)
    return {
        "command": result.command,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "policy_violation": result.policy_violation,
    }
