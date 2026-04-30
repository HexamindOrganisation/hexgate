"""Helpers for evaluating file-scope rules in tool policies."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from fortify.security.models import FileToolPolicy

FILE_TOOL_ARG_NAMES = {
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "glob": "path",
    "grep": "path",
}


def extract_scoped_path(tool_name: str, arguments: dict[str, Any] | None) -> str | None:
    """Return the relevant path argument for a scoped file tool when present."""
    if not arguments:
        return None
    argument_name = FILE_TOOL_ARG_NAMES.get(tool_name)
    if argument_name is None:
        return None
    value = arguments.get(argument_name)
    return value if isinstance(value, str) and value.strip() else None


def _normalize_path(path: str) -> str:
    """Return a normalized relative-style path string for glob matching."""
    normalized = path.replace("\\", "/").strip()
    normalized = normalized.lstrip("./")
    return normalized or "."


def _matches_any(path: str, patterns: list[str]) -> bool:
    """Return whether a normalized path matches any glob pattern."""
    normalized = _normalize_path(path)
    candidate = PurePosixPath(normalized)
    return any(candidate.match(pattern) for pattern in patterns)


def is_path_allowed(
    tool_name: str,
    arguments: dict[str, Any] | None,
    policy: FileToolPolicy,
) -> bool:
    """Return whether the current path is inside the configured file scope."""
    if policy.file_scope is None:
        return True

    target_path = extract_scoped_path(tool_name, arguments)
    if target_path is None:
        return False

    denied_paths = policy.file_scope.denied_paths
    if denied_paths and _matches_any(target_path, denied_paths):
        return False

    allowed_paths = policy.file_scope.allowed_paths
    if allowed_paths:
        return _matches_any(target_path, allowed_paths)

    return True


def build_file_scope_hint(policy: FileToolPolicy) -> dict[str, list[str]] | None:
    """Return a compact hint payload for file-scope-aware policy errors."""
    if policy.file_scope is None:
        return None

    hint: dict[str, list[str]] = {}
    if policy.file_scope.allowed_paths:
        hint["allowed_paths"] = list(policy.file_scope.allowed_paths)
    if policy.file_scope.denied_paths:
        hint["denied_paths"] = list(policy.file_scope.denied_paths)
    return hint or None
