"""Workspace-backed exact-replacement file edit tool."""

from __future__ import annotations

from fortify.runtime import ToolUseContext
from fortify.tools.decorators import agent_tool
from fortify.tools.files._common import display_path, ensure_text_file, require_workspace


def _format_edit_file_call(arguments: dict[str, object]) -> str:
    """Format a compact label for an edit_file invocation."""
    file_path = arguments.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        return f"editing {file_path.strip()}"
    return "editing file"


@agent_tool(
    name="edit_file",
    call_formatter=_format_edit_file_call,
)
async def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    tool_use_context: ToolUseContext,
    replace_all: bool = False,
) -> dict[str, object]:
    """Edit a text file by exact string replacement."""
    workspace = require_workspace(tool_use_context)
    resolved_path = workspace.resolve_path(file_path)
    ensure_text_file(resolved_path)
    original = workspace.read_text(file_path)
    occurrences = original.count(old_string)

    if occurrences == 0:
        raise ValueError("old_string was not found in the target file")
    if occurrences > 1 and not replace_all:
        raise ValueError(
            "old_string matched multiple locations; set replace_all=True to continue"
        )

    updated = (
        original.replace(old_string, new_string)
        if replace_all
        else original.replace(old_string, new_string, 1)
    )
    workspace.write_text(file_path, updated)
    return {
        "file_path": display_path(workspace, resolved_path),
        "num_replacements": occurrences if replace_all else 1,
        "replace_all": replace_all,
    }
