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


def test_sandbox_runtime_config_emits_restrictive_network_defaults(
    tmp_path: Path,
) -> None:
    """Always emit allowUnixSockets and allowLocalBinding with restrictive defaults."""
    config = build_sandbox_runtime_config(tmp_path)

    network = config["network"]
    assert network["allowUnixSockets"] == []
    assert network["allowLocalBinding"] is False


def test_sandbox_runtime_config_passes_unix_sockets_through(tmp_path: Path) -> None:
    """Operator-supplied Unix-domain socket allowlist flows into the config."""
    nested_socket = tmp_path / "agent.sock"

    config = build_sandbox_runtime_config(
        tmp_path,
        allow_unix_sockets=["/var/run/docker.sock", nested_socket],
    )

    network = config["network"]
    assert network["allowUnixSockets"] == [
        "/var/run/docker.sock",
        str(nested_socket),
    ]


def test_sandbox_runtime_config_preserves_socket_symlinks(tmp_path: Path) -> None:
    """Socket paths must round-trip literally so connect() syscalls match.

    If we canonicalized a symlinked socket path, the sandbox filter would
    compare against the resolved path while the program passes the literal
    to connect(); the policy would silently miss.
    """
    real_socket = tmp_path / "real.sock"
    real_socket.touch()
    symlinked = tmp_path / "linked.sock"
    symlinked.symlink_to(real_socket)

    config = build_sandbox_runtime_config(
        tmp_path,
        allow_unix_sockets=[symlinked],
    )

    assert config["network"]["allowUnixSockets"] == [str(symlinked)]


def test_sandbox_runtime_config_resolves_relative_socket_paths(
    tmp_path: Path,
) -> None:
    """Relative socket paths anchor on the workspace root for convenience."""
    config = build_sandbox_runtime_config(
        tmp_path,
        allow_unix_sockets=["sockets/agent.sock"],
    )

    assert config["network"]["allowUnixSockets"] == [
        str(tmp_path / "sockets" / "agent.sock"),
    ]


def test_sandbox_runtime_config_passes_local_binding_opt_in(tmp_path: Path) -> None:
    """Operators can opt in to localhost binding."""
    config = build_sandbox_runtime_config(tmp_path, allow_local_binding=True)

    assert config["network"]["allowLocalBinding"] is True


def test_local_workspace_default_network_block_is_restrictive(tmp_path: Path) -> None:
    """LocalWorkspace defaults inherit the restrictive sandbox defaults."""
    workspace = LocalWorkspace(tmp_path)

    network = workspace.to_sandbox_runtime_config()["network"]
    assert network["allowUnixSockets"] == []
    assert network["allowLocalBinding"] is False


def test_local_workspace_passes_unix_sockets_and_binding_through(
    tmp_path: Path,
) -> None:
    """LocalWorkspace exposes the new sandbox knobs as constructor args."""
    workspace = LocalWorkspace(
        tmp_path,
        allow_unix_sockets=["/var/run/docker.sock"],
        allow_local_binding=True,
    )

    network = workspace.to_sandbox_runtime_config()["network"]
    assert network["allowUnixSockets"] == ["/var/run/docker.sock"]
    assert network["allowLocalBinding"] is True
