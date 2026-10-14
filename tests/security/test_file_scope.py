"""Tests for how a file_scope matches the path a file tool is called with."""

from __future__ import annotations

import pytest

from hexgate.security.file_scope import is_path_allowed
from hexgate.security.models import FileToolPolicy


def _scope(
    allowed: list[str] | None = None, denied: list[str] | None = None
) -> FileToolPolicy:
    return FileToolPolicy.model_validate(
        {
            "mode": "allow",
            "file_scope": {
                "allowed_paths": allowed or [],
                "denied_paths": denied or [],
            },
        }
    )


def _allowed(policy: FileToolPolicy, path: str, tool: str = "read_file") -> bool:
    arg = "path" if tool in ("glob", "grep") else "file_path"
    return is_path_allowed(tool, {arg: path}, policy)


@pytest.mark.parametrize(
    "path",
    [
        "../docs/x.md",  # climbs out, then back into a folder named docs
        "docs/../../docs/x.md",
        "/etc/docs/x.md",  # absolute path ending like the pattern
        "evil/docs/intro.md",  # the pattern matched only the end of the path
        "docs/sub/x.md",  # * stays within one folder
    ],
)
def test_allowed_paths_match_the_whole_path(path: str) -> None:
    assert not _allowed(_scope(allowed=["docs/*.md"]), path)


@pytest.mark.parametrize(
    "path", ["docs/intro.md", "./docs/intro.md", "docs/./intro.md"]
)
def test_allowed_paths_accept_paths_inside(path: str) -> None:
    assert _allowed(_scope(allowed=["docs/*.md"]), path)


@pytest.mark.parametrize(
    "path", ["notes/a.md", "notes/sub/b.md", "notes/sub/deeper/c.md"]
)
def test_double_star_spans_any_depth(path: str) -> None:
    assert _allowed(_scope(allowed=["notes/**"]), path)


def test_double_star_covers_the_folder_itself_for_search_tools() -> None:
    assert _allowed(_scope(allowed=["notes/**"]), "notes", tool="grep")
    assert not _allowed(_scope(allowed=["notes/**"]), "notesx", tool="grep")


def test_denied_paths_reach_nested_files_and_collapse_dot_dot() -> None:
    policy = _scope(allowed=["notes/**"], denied=["notes/private/**"])
    assert not _allowed(policy, "notes/private/deep/key.txt")
    assert not _allowed(policy, "notes/public/../private/key.txt")
    assert _allowed(policy, "notes/public/a.md")


def test_a_path_that_climbs_out_is_denied_even_with_only_denied_paths() -> None:
    assert not _allowed(_scope(denied=["secrets/**"]), "../outside.txt")
    assert _allowed(_scope(denied=["secrets/**"]), "docs/a.md")


def test_absolute_patterns_match_absolute_paths() -> None:
    policy = _scope(allowed=["/tmp/**"])
    assert _allowed(policy, "/tmp/a/b.txt")
    assert not _allowed(policy, "/var/tmp/a.txt")
    assert not _allowed(policy, "/tmp/../etc/passwd")


def test_dot_folders_keep_their_dot() -> None:
    # The old matcher stripped every leading "." and "/", so ".secrets/key"
    # was matched as "secrets/key".
    assert not _allowed(_scope(allowed=["secrets/*"]), ".secrets/key")
