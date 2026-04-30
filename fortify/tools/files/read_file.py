"""Workspace-backed file reading tool."""

from __future__ import annotations

from fortify.runtime import ToolUseContext
from fortify.tools.decorators import agent_tool
from fortify.tools.files._common import display_path, ensure_text_file, require_workspace


def _format_read_file_call(arguments: dict[str, object]) -> str:
    """Format a compact label for a read_file invocation."""
    file_path = arguments.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        return f"reading {file_path.strip()}"
    return "reading file"


@agent_tool(
    name="read_file",
    call_formatter=_format_read_file_call,
)
async def read_file(
    file_path: str,
    tool_use_context: ToolUseContext,
) -> dict[str, object]:
    """Read a text file from the active workspace."""
    workspace = require_workspace(tool_use_context)
    resolved_path = workspace.resolve_path(file_path)
    ensure_text_file(resolved_path)
    content = workspace.read_text(file_path)
    return {
        "file_path": display_path(workspace, resolved_path),
        "content": content,
        "num_lines": len(content.splitlines()),
    }
