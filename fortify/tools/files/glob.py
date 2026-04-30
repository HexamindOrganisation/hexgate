"""Workspace-backed glob search tool."""

from __future__ import annotations

from fortify.runtime import ToolUseContext
from fortify.tools.decorators import agent_tool
from fortify.tools.files._common import cap_results, require_workspace

DEFAULT_GLOB_LIMIT = 100


def _format_glob_call(arguments: dict[str, object]) -> str:
    """Format a compact label for a glob invocation."""
    pattern = arguments.get("pattern")
    if isinstance(pattern, str) and pattern.strip():
        return f"glob {pattern.strip()}"
    return "globbing files"


@agent_tool(
    name="glob",
    call_formatter=_format_glob_call,
)
async def glob(
    pattern: str,
    tool_use_context: ToolUseContext,
    path: str | None = None,
    limit: int = DEFAULT_GLOB_LIMIT,
) -> dict[str, object]:
    """Find files matching a glob pattern within the active workspace."""
    workspace = require_workspace(tool_use_context)
    search_root = workspace.root_dir if path is None else workspace.resolve_path(path)
    matches = [
        str(match.relative_to(workspace.root_dir))
        for match in search_root.glob(pattern)
        if match.is_file()
    ]
    matches.sort()
    capped_matches, truncated = cap_results(matches, limit)
    return {
        "pattern": pattern,
        "filenames": capped_matches,
        "num_files": len(capped_matches),
        "truncated": truncated,
        "search_root": str(search_root.relative_to(workspace.root_dir))
        if search_root != workspace.root_dir
        else ".",
    }
