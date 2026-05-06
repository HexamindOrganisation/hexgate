"""Tests for workspace sandbox-runtime config generation."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from fortify.runtime import LocalWorkspace, build_sandbox_runtime_config
from fortify.runtime import workspace as workspace_module
from fortify.runtime.srt import SrtUnavailableError
from fortify.runtime.workspace import _build_sandbox_env


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


# ---------------------------------------------------------------------------
# Env construction helper.
# ---------------------------------------------------------------------------


def test_build_sandbox_env_starts_from_a_fixed_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sandbox env always carries PATH, HOME, TMPDIR, TERM regardless of parent."""
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TERM", raising=False)

    env = _build_sandbox_env(tmp_path, {})

    assert "PATH" in env and env["PATH"]
    assert env["HOME"] == str(tmp_path)
    assert env["TMPDIR"] == "/tmp"
    assert env["TERM"] == "dumb"


def test_build_sandbox_env_drops_secret_like_keys_from_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parent env carries secrets we must not inherit into the sandbox."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xxxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xxxx")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxxx")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent")

    env = _build_sandbox_env(tmp_path, {})

    for leaky in (
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "SSH_AUTH_SOCK",
    ):
        assert leaky not in env, f"{leaky} leaked into sandbox env"


def test_build_sandbox_env_passes_locale_keys_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locale env keys affect formatting and are safe to inherit."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    env = _build_sandbox_env(tmp_path, {})

    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "en_US.UTF-8"


def test_build_sandbox_env_extra_env_overrides_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator-supplied extra_env wins over baseline defaults."""
    env = _build_sandbox_env(
        tmp_path,
        {"PATH": "/custom/bin", "NODE_ENV": "test"},
    )

    assert env["PATH"] == "/custom/bin"
    assert env["NODE_ENV"] == "test"
    # Other defaults remain.
    assert env["HOME"] == str(tmp_path)


# ---------------------------------------------------------------------------
# run_command: srt invocation wire-shape (mocked subprocess).
# ---------------------------------------------------------------------------


class _FakeAsyncProcess:
    """Stand-in for asyncio.subprocess.Process for tests that mock spawn."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        communicate_delay: float = 0.0,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = os.getpid()  # any real pid; getpgid won't fail on our own
        self._communicate_delay = communicate_delay

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._communicate_delay:
            await asyncio.sleep(self._communicate_delay)
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode


def _patch_srt_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend `srt` is installed at a fixed path."""
    monkeypatch.setattr(workspace_module, "ensure_srt_available", lambda: None)


def _patch_subprocess_exec(
    monkeypatch: pytest.MonkeyPatch,
    *,
    process: _FakeAsyncProcess | None = None,
    raises: BaseException | None = None,
) -> dict[str, Any]:
    """Capture the argv/kwargs/JSON settings for the spawned child."""
    captured: dict[str, Any] = {}

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeAsyncProcess:
        captured["argv"] = argv
        captured["kwargs"] = dict(kwargs)
        # Snapshot the settings file before run_command unlinks it.
        if "--settings" in argv:
            settings_path = argv[argv.index("--settings") + 1]
            captured["settings_path"] = settings_path
            with open(settings_path) as fh:
                captured["settings"] = json.load(fh)
        if raises is not None:
            raise raises
        return process or _FakeAsyncProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


@pytest.mark.asyncio
async def test_run_command_spawns_srt_with_settings_and_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_command should exec `srt --settings <json> -- sh -c <command>`."""
    _patch_srt_present(monkeypatch)
    captured = _patch_subprocess_exec(monkeypatch)

    workspace = LocalWorkspace(tmp_path)
    await workspace.run_command("echo hi")

    argv = captured["argv"]
    assert argv[0] == "srt"
    assert argv[1] == "--settings"
    # argv[2] is a temp path; assert via the captured snapshot below.
    assert argv[3] == "--"
    assert argv[4:] == ("sh", "-c", "echo hi")
    assert "filesystem" in captured["settings"]
    assert "network" in captured["settings"]


@pytest.mark.asyncio
async def test_run_command_starts_new_session_for_pgroup_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child must be in its own session so we can SIGTERM the whole group."""
    _patch_srt_present(monkeypatch)
    captured = _patch_subprocess_exec(monkeypatch)

    workspace = LocalWorkspace(tmp_path)
    await workspace.run_command("true")

    assert captured["kwargs"].get("start_new_session") is True


@pytest.mark.asyncio
async def test_run_command_settings_file_contains_resolved_workspace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The JSON written for srt encodes the workspace's allow/deny lists."""
    _patch_srt_present(monkeypatch)
    captured = _patch_subprocess_exec(monkeypatch)

    workspace = LocalWorkspace(
        tmp_path,
        allowed_domains=["example.com"],
        deny_write_paths=[".env"],
    )
    await workspace.run_command("true")

    settings = captured["settings"]
    assert str(tmp_path.resolve()) in settings["filesystem"]["allowWrite"]
    assert settings["filesystem"]["denyWrite"] == [str((tmp_path / ".env").resolve())]
    assert settings["network"]["allowedDomains"] == ["example.com"]


@pytest.mark.asyncio
async def test_run_command_unlinks_settings_file_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp config file must not survive a successful run."""
    _patch_srt_present(monkeypatch)
    captured = _patch_subprocess_exec(monkeypatch)

    workspace = LocalWorkspace(tmp_path)
    await workspace.run_command("true")

    assert not os.path.exists(captured["settings_path"])


@pytest.mark.asyncio
async def test_run_command_unlinks_settings_file_on_spawn_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If exec itself fails, the temp file should still be removed."""
    _patch_srt_present(monkeypatch)
    captured = _patch_subprocess_exec(
        monkeypatch, raises=FileNotFoundError("srt vanished")
    )

    workspace = LocalWorkspace(tmp_path)
    with pytest.raises(FileNotFoundError):
        await workspace.run_command("true")

    # The fake_exec captured the path before raising.
    assert "settings_path" in captured
    assert not os.path.exists(captured["settings_path"])


@pytest.mark.asyncio
async def test_run_command_fails_closed_when_srt_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `srt` on PATH => SrtUnavailableError, no fallback to host shell."""

    def boom() -> None:
        raise SrtUnavailableError("not installed")

    monkeypatch.setattr(workspace_module, "ensure_srt_available", boom)

    # Trip-wire: the subprocess layer must NOT be reached in this path.
    async def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("subprocess spawned despite missing srt")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", must_not_run)

    workspace = LocalWorkspace(tmp_path)
    with pytest.raises(SrtUnavailableError):
        await workspace.run_command("true")


@pytest.mark.asyncio
async def test_run_command_passes_scrubbed_env_to_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env reaching the child has only the explicit allowlist + extras."""
    _patch_srt_present(monkeypatch)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xxxx")
    captured = _patch_subprocess_exec(monkeypatch)

    workspace = LocalWorkspace(tmp_path, extra_env={"NODE_ENV": "test"})
    await workspace.run_command("true")

    env = captured["kwargs"]["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["NODE_ENV"] == "test"
    assert env["HOME"] == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_run_command_returns_command_result_from_fake_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end shape check: stdout/stderr/exit_code flow through unchanged."""
    _patch_srt_present(monkeypatch)
    process = _FakeAsyncProcess(stdout=b"hello\n", stderr=b"warn\n", returncode=2)
    _patch_subprocess_exec(monkeypatch, process=process)

    workspace = LocalWorkspace(tmp_path)
    result = await workspace.run_command("noop")

    assert result.command == "noop"
    assert result.exit_code == 2
    assert result.stdout == "hello\n"
    assert result.stderr == "warn\n"
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


@pytest.mark.asyncio
async def test_run_command_unlinks_settings_file_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tempfile cleanup happens even when wait_for raises TimeoutError."""
    _patch_srt_present(monkeypatch)
    process = _FakeAsyncProcess(communicate_delay=10.0)
    captured = _patch_subprocess_exec(monkeypatch, process=process)

    # Stub out the kill path: getpgid + killpg should not actually signal
    # this test process. We only care that cleanup runs.
    monkeypatch.setattr(workspace_module.os, "getpgid", lambda pid: pid)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        workspace_module.os,
        "killpg",
        lambda pgid, sig: killed.append((pgid, sig)),
    )

    workspace = LocalWorkspace(tmp_path)
    with pytest.raises(TimeoutError):
        await workspace.run_command("noop", timeout_seconds=0)

    assert not os.path.exists(captured["settings_path"])
    # SIGTERM was attempted on the fake process group.
    assert any(sig == signal.SIGTERM for _, sig in killed)


# ---------------------------------------------------------------------------
# Command allowlist integration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_default_workspace_runs_anything(tmp_path: Path) -> None:
    """Without an allowlist, run_command behavior is unchanged (back-compat)."""
    workspace = LocalWorkspace(tmp_path)

    result = await workspace.run_command("echo ok")

    assert result.exit_code == 0
    assert result.policy_violation is False
    assert "ok" in result.stdout


@pytest.mark.asyncio
async def test_run_command_allowed_by_policy_executes(tmp_path: Path) -> None:
    """A command on the allowlist executes and reports policy_violation=False."""
    workspace = LocalWorkspace(tmp_path, allowed_commands=["echo"])

    result = await workspace.run_command("echo hello")

    assert result.exit_code == 0
    assert result.policy_violation is False
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_run_command_rejected_by_policy_does_not_execute(
    tmp_path: Path,
) -> None:
    """A disallowed command returns 126 + policy_violation without spawning a shell."""
    sentinel = tmp_path / "marker"
    workspace = LocalWorkspace(tmp_path, allowed_commands=["echo"])

    # If policy enforcement bypasses the shell, this `touch` never runs.
    result = await workspace.run_command(f"touch {sentinel}")

    assert result.exit_code == 126
    assert result.policy_violation is True
    assert "touch" in result.stderr
    assert sentinel.exists() is False


@pytest.mark.asyncio
async def test_run_command_rejects_pipeline_with_one_disallowed_leg(
    tmp_path: Path,
) -> None:
    """Composite shell pipelines fail closed if any leg is disallowed."""
    workspace = LocalWorkspace(tmp_path, allowed_commands=["echo"])

    result = await workspace.run_command("echo hi | xargs -I{} curl {}")

    assert result.exit_code == 126
    assert result.policy_violation is True


@pytest.mark.asyncio
async def test_run_command_rejects_eval_even_if_allowlisted(tmp_path: Path) -> None:
    """`eval` is statically banned regardless of allowlist content."""
    workspace = LocalWorkspace(tmp_path, allowed_commands=["eval", "echo"])

    result = await workspace.run_command('eval "echo $PATH"')

    assert result.exit_code == 126
    assert result.policy_violation is True
    assert "eval" in result.stderr


@pytest.mark.asyncio
async def test_run_command_allow_command_substitution_opt_in(
    tmp_path: Path,
) -> None:
    """allow_command_substitution=True permits $(...) when its inner command is allowed."""
    workspace = LocalWorkspace(
        tmp_path,
        allowed_commands=["echo"],
        allow_command_substitution=True,
    )

    result = await workspace.run_command("echo $(echo nested)")

    assert result.exit_code == 0
    assert result.policy_violation is False
    assert "nested" in result.stdout


@pytest.mark.asyncio
async def test_run_command_command_substitution_blocked_by_default(
    tmp_path: Path,
) -> None:
    """By default, $(...) is rejected even when its inner command is allowed."""
    workspace = LocalWorkspace(tmp_path, allowed_commands=["echo"])

    result = await workspace.run_command("echo $(echo nested)")

    assert result.exit_code == 126
    assert result.policy_violation is True
