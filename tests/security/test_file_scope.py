"""Tests for how a file_scope decides which paths a file tool may reach.

Two modes: with the run's workspace the path is resolved as the file tools
resolve it (root-relative, symlinks followed); without one the check is lexical
and refuses what it can't pin down. Each bypass found in review has a case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hexgate.runtime import LocalWorkspace
from hexgate.runtime.context import (
    ToolUseContext,
    reset_current_tool_use_context,
    set_current_tool_use_context,
)
from hexgate.security.decision import DecisionOutcome
from hexgate.security.file_scope import is_path_allowed
from hexgate.security.models import AgentPolicy, FileToolPolicy
from hexgate.security.policy import evaluate_tool_call


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


def _ok(
    policy: FileToolPolicy,
    path: str,
    tool: str = "read_file",
    workspace: LocalWorkspace | None = None,
) -> bool:
    arg = "path" if tool in ("glob", "grep") else "file_path"
    return is_path_allowed(tool, {arg: path}, policy, workspace=workspace)


@pytest.fixture
def ws(tmp_path: Path) -> LocalWorkspace:
    for rel in (
        "docs/a.md",
        "docs/sub/b.md",
        "notes/n.md",
        "secrets/key",
        "keys/s.pem",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    (tmp_path / "notes" / "private").mkdir()
    (tmp_path / "notes" / "private" / "p.txt").write_text("x")
    (tmp_path / "docs" / "link").symlink_to(tmp_path / "secrets")
    return LocalWorkspace(root_dir=tmp_path)


# ---- lexical mode (no workspace) ---------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../docs/x.md",
        "docs/../docs/x.md",  # any ".." is refused without the filesystem
        "/etc/docs/x.md",  # absolute, can't be mapped to the root
        "evil/docs/intro.md",  # anchored patterns match from the root
        "docs/sub/x.md",  # * stays within one name
        " docs/x.md",  # padding the tool would keep
        "docs\\x.md",  # a backslash is part of the name on POSIX
        "~/docs/x.md",
    ],
)
def test_lexical_allow_list_refuses_what_it_cannot_pin_down(path: str) -> None:
    assert not _ok(_scope(allowed=["docs/*.md"]), path)


@pytest.mark.parametrize("path", ["docs/intro.md", "./docs/intro.md", "docs//intro.md"])
def test_lexical_allow_list_accepts_paths_inside(path: str) -> None:
    assert _ok(_scope(allowed=["docs/*.md"]), path)


def test_double_star_spans_any_depth() -> None:
    assert _ok(_scope(allowed=["notes/**"]), "notes/sub/deeper/c.md")


def test_slash_free_patterns_match_at_any_depth_on_both_sides() -> None:
    # Like .gitignore: "*.pem" or "secrets" without a "/" matches anywhere.
    assert not _ok(_scope(denied=["*.pem"]), "keys/server.pem")
    assert not _ok(_scope(denied=["secrets"]), "app/secrets/key")
    assert _ok(_scope(allowed=["*.md"]), "a/b/c.md")


def test_a_denied_folder_covers_everything_under_it() -> None:
    assert not _ok(_scope(denied=["notes/private"]), "notes/private/deep/k.txt")


def test_relative_deny_list_refuses_an_absolute_path_it_cannot_map() -> None:
    # main denied this; the first version of this fix let it through.
    assert not _ok(_scope(denied=["secrets/**"]), "/home/u/ws/secrets/key")


def test_double_slash_does_not_dodge_an_absolute_deny() -> None:
    assert not _ok(_scope(denied=["/ws/secrets/**"]), "//ws/secrets/key")


# ---- search tools --------------------------------------------------------------


def test_search_on_a_folder_with_a_denied_subfolder_is_denied() -> None:
    policy = _scope(allowed=["notes/**"], denied=["notes/private/**"])
    assert not _ok(policy, "notes", tool="grep")
    assert not _ok(policy, ".", tool="glob")
    assert _ok(policy, "notes/public", tool="grep")


def test_search_needs_the_whole_folder_allowed() -> None:
    assert _ok(_scope(allowed=["notes/**"]), "notes", tool="grep")
    assert not _ok(_scope(allowed=["notes/*.md"]), "notes", tool="grep")
    assert not _ok(_scope(allowed=["notes/**"]), "notesx", tool="grep")


def test_search_is_denied_when_a_slash_free_deny_could_match_inside() -> None:
    assert not _ok(_scope(denied=["*.pem"]), "keys", tool="grep")


# ---- resolved mode (the run's workspace) ---------------------------------------


def test_absolute_path_inside_the_workspace_is_judged_relative_to_its_root(
    ws: LocalWorkspace,
) -> None:
    secret = str(ws.root_dir / "secrets" / "key")
    assert not _ok(_scope(denied=["secrets/**"]), secret, workspace=ws)
    doc = str(ws.root_dir / "docs" / "a.md")
    assert _ok(_scope(allowed=["docs/*.md"]), doc, workspace=ws)


def test_symlink_is_judged_by_its_target(ws: LocalWorkspace) -> None:
    # docs/link -> secrets: the text says docs/, the file opened is secrets/key.
    assert not _ok(_scope(allowed=["docs/**"]), "docs/link/key", workspace=ws)
    assert not _ok(_scope(denied=["secrets/**"]), "docs/link/key", workspace=ws)


def test_dot_dot_is_resolved_against_the_real_tree(ws: LocalWorkspace) -> None:
    policy = _scope(allowed=["docs/**"])
    assert _ok(policy, "docs/sub/../a.md", workspace=ws)
    assert not _ok(policy, "docs/link/../notes/n.md", workspace=ws)


def test_a_path_outside_the_workspace_is_denied(ws: LocalWorkspace) -> None:
    assert not _ok(_scope(denied=["secrets/**"]), "../outside.txt", workspace=ws)
    assert not _ok(_scope(allowed=["**"]), "/etc/passwd", workspace=ws)


def test_evaluate_tool_call_uses_the_running_agents_workspace(
    ws: LocalWorkspace,
) -> None:
    policy = AgentPolicy.model_validate(
        {
            "default_policy": {"mode": "deny"},
            "tools": {
                "read_file": {
                    "mode": "allow",
                    "file_scope": {"denied_paths": ["secrets/**"]},
                }
            },
        }
    )
    args = {"file_path": str(ws.root_dir / "secrets" / "key")}
    token = set_current_tool_use_context(ToolUseContext(workspace=ws))
    try:
        verdict = evaluate_tool_call(policy, "read_file", args)
    finally:
        reset_current_tool_use_context(token)
    assert verdict.outcome is DecisionOutcome.DENY
