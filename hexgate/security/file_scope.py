"""Helpers for evaluating file-scope rules in tool policies."""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath
from typing import Any

from hexgate.security.models import FileToolPolicy

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


def _normalize_path(path: str) -> PurePosixPath | None:
    """Collapse ``.`` and ``..`` segments, or ``None`` when the path climbs above
    where it starts (``../x``), which no relative pattern may cover."""
    normalized = posixpath.normpath(path.replace("\\", "/").strip())
    if normalized == ".." or normalized.startswith("../"):
        return None
    return PurePosixPath(normalized)


def _matches(path: PurePosixPath, pattern: str) -> bool:
    """Match the whole path, from its start, against one glob pattern.

    ``*`` stays within a segment and ``**`` spans any number of them. A relative
    pattern never matches an absolute path. ``dir/**`` also matches ``dir``
    itself, so a scoped ``glob`` or ``grep`` may search the folder it names.
    """
    if path.full_match(pattern):
        return True
    return pattern.endswith("/**") and path.full_match(pattern[: -len("/**")])


def _matches_any(path: PurePosixPath, patterns: list[str]) -> bool:
    return any(_matches(path, pattern) for pattern in patterns)


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

    # A path that climbs out of the scope's root is outside every scope.
    path = _normalize_path(target_path)
    if path is None:
        return False

    denied_paths = policy.file_scope.denied_paths
    if denied_paths and _matches_any(path, denied_paths):
        return False

    allowed_paths = policy.file_scope.allowed_paths
    if allowed_paths:
        return _matches_any(path, allowed_paths)

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
