"""Workspace-backed file writing tool."""

from __future__ import annotations

from coolagents.runtime import ToolUseContext
from coolagents.tools.decorators import agent_tool
from coolagents.tools.files._common import display_path, require_workspace


def _format_write_file_call(arguments: dict[str, object]) -> str:
    """Format a compact label for a write_file invocation."""
    file_path = arguments.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        return f"writing {file_path.strip()}"
    return "writing file"


@agent_tool(
    name="write_file",
    call_formatter=_format_write_file_call,
)
async def write_file(
    file_path: str,
    content: str,
    tool_use_context: ToolUseContext,
) -> dict[str, object]:
    """Create or overwrite a text file inside the active workspace."""
    workspace = require_workspace(tool_use_context)
    resolved_path = workspace.resolve_path(file_path)
    existed = resolved_path.exists()
    workspace.write_text(file_path, content)
    return {
        "file_path": display_path(workspace, resolved_path),
        "operation": "update" if existed else "create",
        "num_lines": len(content.splitlines()),
    }
