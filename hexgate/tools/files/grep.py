"""Workspace-backed text search tool."""

from __future__ import annotations

import fnmatch
import re
from typing import Literal

from hexgate.runtime import ToolUseContext
from hexgate.tools.decorators import agent_tool
from hexgate.tools.files._common import cap_results, require_workspace

DEFAULT_GREP_LIMIT = 100


def _format_grep_call(arguments: dict[str, object]) -> str:
    """Format a compact label for a grep invocation."""
    pattern = arguments.get("pattern")
    if isinstance(pattern, str) and pattern.strip():
        return f"grep {pattern.strip()}"
    return "searching file contents"


def _iter_candidate_files(root, pattern: str | None = None) -> list:
    """Return candidate files under a root, optionally filtered by glob."""
    files = [path for path in root.rglob("*") if path.is_file()]
    if pattern is None:
        return files
    return [path for path in files if fnmatch.fnmatch(path.name, pattern)]


@agent_tool(
    name="grep",
    call_formatter=_format_grep_call,
)
async def grep(
    pattern: str,
    tool_use_context: ToolUseContext,
    path: str | None = None,
    glob: str | None = None,
    output_mode: Literal[
        "files_with_matches", "content", "count"
    ] = "files_with_matches",
    case_insensitive: bool = False,
    head_limit: int = DEFAULT_GREP_LIMIT,
) -> dict[str, object]:
    """Search file contents within the active workspace."""
    workspace = require_workspace(tool_use_context)
    search_root = workspace.root_dir if path is None else workspace.resolve_path(path)
    flags = re.IGNORECASE if case_insensitive else 0
    regex = re.compile(pattern, flags)

    matching_files: list[str] = []
    content_lines: list[str] = []
    match_count = 0

    for file_path in _iter_candidate_files(search_root, glob):
        relative_path = str(file_path.relative_to(workspace.root_dir))
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        file_had_match = False
        for line_number, line in enumerate(content.splitlines(), start=1):
            if regex.search(line):
                file_had_match = True
                match_count += 1
                if output_mode == "content":
                    content_lines.append(f"{relative_path}:{line_number}:{line}")
        if file_had_match:
            matching_files.append(relative_path)

    matching_files.sort()
    capped_files, files_truncated = cap_results(matching_files, head_limit)
    capped_content, content_truncated = cap_results(content_lines, head_limit)
    truncated = files_truncated or content_truncated

    payload: dict[str, object] = {
        "pattern": pattern,
        "mode": output_mode,
        "num_files": len(matching_files),
        "num_matches": match_count,
        "filenames": capped_files,
        "truncated": truncated,
    }
    if output_mode == "content":
        payload["content"] = "\n".join(capped_content)
    elif output_mode == "count":
        payload["count"] = match_count
    return payload
