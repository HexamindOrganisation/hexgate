"""Helpers for evaluating file-scope rules in tool policies.

A ``file_scope`` is checked against the path the tool will actually open, not
the string the model wrote. When a workspace is active (every native agent
run), the path is resolved the way the workspace resolves it (against its
root, following symlinks) and matched relative to that root, so every spelling
of one file (``./a``, ``a/../a``, ``/abs/root/a``, a symlink) is the same
candidate. Without a workspace (a dry-run, or a framework agent with its own
file tools), the check is lexical and refuses whatever it can't be sure of:
``..`` segments, absolute paths it can't map to a root, backslashes, ``~`` and
padding whitespace.

Patterns follow ``.gitignore``: one without a ``/`` matches a file or folder
name at any depth (``*.pem``, ``secrets``); one with a ``/`` is anchored at the
root (``docs/*.md``) or, starting with ``/``, at the filesystem root. A pattern
that matches a folder also matches everything under it. ``*`` stays within one
name and ``**`` spans any number of them.

``glob`` and ``grep`` read a whole folder, so they are judged on everything
under it: allowed only when the folder is allowed as a whole, and denied when
any denied pattern could match something inside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from hexgate.security.models import FileToolPolicy

if TYPE_CHECKING:
    from hexgate.runtime.workspace import Workspace

FILE_TOOL_ARG_NAMES = {
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "glob": "path",
    "grep": "path",
}

# Tools whose path names a folder they read recursively.
SEARCH_TOOLS = frozenset({"glob", "grep"})


def extract_scoped_path(tool_name: str, arguments: dict[str, Any] | None) -> str | None:
    """Return the relevant path argument for a scoped file tool when present."""
    if not arguments:
        return None
    argument_name = FILE_TOOL_ARG_NAMES.get(tool_name)
    if argument_name is None:
        return None
    value = arguments.get(argument_name)
    return value if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class _Candidate:
    """One path in the two forms patterns are matched against."""

    relative: tuple[str, ...] | None  # names under the root; None if not under it
    absolute: tuple[str, ...] | None  # names under "/"; None if unknown


def _candidate(raw: str, workspace: Workspace | None) -> _Candidate | None:
    """The path the tool will open, or ``None`` when it can't be pinned down
    (which denies the call)."""
    if raw != raw.strip() or "\\" in raw or "\x00" in raw or raw.startswith("~"):
        return None
    if workspace is not None:
        return _resolved(raw, workspace)
    return _lexical(raw)


def _resolved(raw: str, workspace: Workspace) -> _Candidate | None:
    try:
        resolved = workspace.resolve_path(raw)  # follows symlinks, stays in root
        relative = resolved.relative_to(workspace.root_dir)
    except (ValueError, OSError, RuntimeError):
        return None
    return _Candidate(
        relative=_names(relative.as_posix()), absolute=_names(resolved.as_posix())
    )


def _lexical(raw: str) -> _Candidate | None:
    names = _names(raw)
    if ".." in names:
        # Can't collapse safely without the filesystem: a symlink before the
        # ".." changes which folder it climbs out of.
        return None
    if raw.startswith("/"):
        return _Candidate(relative=None, absolute=names)
    return _Candidate(relative=names, absolute=None)


def _names(path: str) -> tuple[str, ...]:
    return tuple(name for name in path.split("/") if name not in ("", "."))


def _pattern_names(pattern: str) -> tuple[bool, tuple[str, ...]]:
    """``(absolute, names)``, with a slash-free pattern made to match at any depth."""
    if pattern.startswith("/"):
        return True, _names(pattern)
    names = _names(pattern)
    if "/" not in pattern.strip("/"):
        names = ("**", *names)
    return False, names


def _full_match(names: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not names:
        return all(p == "**" for p in pattern)
    return PurePosixPath(*names).full_match("/".join(pattern) or ".")


def _matches(candidate: _Candidate, pattern: str) -> bool:
    """The path, or a folder above it, matches ``pattern``."""
    absolute, pat = _pattern_names(pattern)
    names = candidate.absolute if absolute else candidate.relative
    if names is None:
        return False
    return any(_full_match(names[:end], pat) for end in range(1, len(names) + 1))


def _could_match_inside(candidate: _Candidate, pattern: str) -> bool:
    """Something under the folder ``candidate`` could match ``pattern``."""
    absolute, pat = _pattern_names(pattern)
    names = candidate.absolute if absolute else candidate.relative
    if names is None:
        return False
    for index, name in enumerate(names):
        if index >= len(pat):
            return False  # the pattern ends above the folder: _matches covers it
        if pat[index] == "**":
            return True
        if not fnmatchcase(name, pat[index]):
            return False
    return len(pat) > len(names)


def _covers_whole_folder(candidate: _Candidate, pattern: str) -> bool:
    """Everything under the folder ``candidate`` matches ``pattern``."""
    if _matches(candidate, pattern):
        return True
    absolute, pat = _pattern_names(pattern)
    names = candidate.absolute if absolute else candidate.relative
    if names is None or not pat or pat[-1] != "**":
        return False
    return _full_match(names, pat[:-1]) if pat[:-1] else True


def _unknown_form(candidate: _Candidate, patterns: list[str]) -> bool:
    """Some pattern can't be judged: the path has no form for its kind."""
    for pattern in patterns:
        absolute, _ = _pattern_names(pattern)
        if (candidate.absolute if absolute else candidate.relative) is None:
            return True
    return False


def is_path_allowed(
    tool_name: str,
    arguments: dict[str, Any] | None,
    policy: FileToolPolicy,
    *,
    workspace: Workspace | None = None,
) -> bool:
    """Return whether the current path is inside the configured file scope.

    ``workspace`` is the run's workspace, when there is one: the path is then
    resolved exactly as the file tools resolve it. Anything that can't be
    resolved, or can't be judged against every pattern, is denied.
    """
    if policy.file_scope is None:
        return True

    target_path = extract_scoped_path(tool_name, arguments)
    if target_path is None:
        return False

    candidate = _candidate(target_path, workspace)
    if candidate is None:
        return False

    denied = policy.file_scope.denied_paths
    allowed = policy.file_scope.allowed_paths
    # A deny pattern we can't evaluate for this path could be the one that
    # matches it, so it denies.
    if _unknown_form(candidate, denied):
        return False

    search = tool_name in SEARCH_TOOLS
    if any(_matches(candidate, p) for p in denied):
        return False
    if search and any(_could_match_inside(candidate, p) for p in denied):
        return False

    if allowed:
        if search:
            return any(_covers_whole_folder(candidate, p) for p in allowed)
        return any(_matches(candidate, p) for p in allowed)
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
