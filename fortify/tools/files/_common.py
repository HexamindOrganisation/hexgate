"""Shared helpers for workspace-backed file tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fortify.runtime import ToolUseContext, Workspace


def require_workspace(tool_use_context: ToolUseContext) -> Workspace:
    """Return the active workspace or raise a clear error."""
    if tool_use_context.workspace is None:
        raise RuntimeError("This tool requires an active workspace.")
    return tool_use_context.workspace


def display_path(workspace: Workspace, resolved_path: Path) -> str:
    """Render a resolved path relative to the workspace root when possible."""
    return str(resolved_path.relative_to(workspace.root_dir))


def ensure_text_file(path: Path) -> None:
    """Reject directories and missing files before text operations."""
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"Expected a file but got a directory: {path}")


def cap_results(items: list[Any], limit: int) -> tuple[list[Any], bool]:
    """Cap a result list and report whether truncation happened."""
    if len(items) <= limit:
        return items, False
    return items[:limit], True
