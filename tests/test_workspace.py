"""Tests for workspace sandbox-runtime config generation."""

from __future__ import annotations

from pathlib import Path

from fortify.runtime import LocalWorkspace, build_sandbox_runtime_config


def test_local_workspace_generates_sandbox_runtime_config(tmp_path: Path) -> None:
    """Include the workspace root in the read and write allowlists."""
    workspace = LocalWorkspace(tmp_path)

    config = workspace.to_sandbox_runtime_config()

    filesystem = config["filesystem"]
    assert filesystem["allowRead"] == [str(tmp_path.resolve())]
    assert filesystem["allowWrite"] == [str(tmp_path.resolve()), "/tmp"]


def test_local_workspace_includes_extra_paths_and_domains(tmp_path: Path) -> None:
    """Map optional workspace settings into the sandbox-runtime config."""
    workspace = LocalWorkspace(
        tmp_path,
        extra_read_paths=["docs", tmp_path / "fixtures"],
        extra_write_paths=["build"],
        deny_write_paths=[".env"],
        allowed_domains=["api.github.com", "api.github.com", "example.com"],
        denied_domains=["malicious.com"],
    )

    config = workspace.to_sandbox_runtime_config()

    filesystem = config["filesystem"]
    network = config["network"]

    assert filesystem["allowRead"] == [
        str(tmp_path.resolve()),
        str((tmp_path / "docs").resolve()),
        str((tmp_path / "fixtures").resolve()),
    ]
    assert filesystem["allowWrite"] == [
        str(tmp_path.resolve()),
        "/tmp",
        str((tmp_path / "build").resolve()),
    ]
    assert filesystem["denyWrite"] == [str((tmp_path / ".env").resolve())]
    assert network["allowedDomains"] == ["api.github.com", "example.com"]
    assert network["deniedDomains"] == ["malicious.com"]


def test_sandbox_runtime_config_uses_conservative_deny_read_defaults(
    tmp_path: Path,
) -> None:
    """Deny broad sensitive regions without emitting a blanket root deny."""
    config = build_sandbox_runtime_config(tmp_path)

    deny_read = config["filesystem"]["denyRead"]

    assert str(Path.home().expanduser().resolve()) in deny_read
    assert "/" not in deny_read
