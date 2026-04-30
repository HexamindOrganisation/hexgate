"""Workspace abstractions for runtime-scoped file access."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from fortify.runtime.sandbox_runtime import build_sandbox_runtime_config
from fortify.runtime.srt import ensure_srt_available

# Locale-style env keys we pass through from the parent if set. They affect
# tool output formatting (date, sort order, error messages) and don't carry
# secrets, so passthrough is safe and saves operators from having to set
# them manually.
_LOCALE_PASSTHROUGH_KEYS = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_COLLATE",
    "LC_MESSAGES",
)

# Conservative default PATH inside the sandbox. Covers the standard system
# binaries plus Homebrew on Apple Silicon. Operators who need pyenv, nvm,
# conda, etc. should layer those in via ``extra_env``.
_DEFAULT_SANDBOX_PATH = ":".join(
    (
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
        "/usr/local/sbin",
        "/usr/sbin",
        "/sbin",
    )
)

_PROCESS_GROUP_GRACE_SECONDS = 2.0


def _build_sandbox_env(
    workspace_root: Path,
    extra_env: Mapping[str, str],
) -> dict[str, str]:
    """Construct the env passed to the sandboxed child.

    Allowlist-based: no parent-process env keys flow through unless they're
    in ``_LOCALE_PASSTHROUGH_KEYS``. Operator-supplied ``extra_env`` overrides
    the defaults, so a caller who genuinely needs e.g. ``NODE_ENV`` can set
    it without disabling the scrub.
    """
    env: dict[str, str] = {
        "PATH": _DEFAULT_SANDBOX_PATH,
        "HOME": str(workspace_root),
        "TMPDIR": "/tmp",
        "TERM": "dumb",
    }
    for key in _LOCALE_PASSTHROUGH_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if extra_env:
        env.update(extra_env)
    return env


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill the child and any descendants it spawned in its session."""
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_GROUP_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await process.wait()
    except ProcessLookupError:
        pass


@dataclass(slots=True)
class CommandResult:
    """Captured result of one workspace-scoped shell command."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class Workspace(ABC):
    """Abstract execution workspace exposed to tools at runtime."""

    @property
    @abstractmethod
    def root_dir(self) -> Path:
        """Return the workspace root directory."""

    @abstractmethod
    def resolve_path(self, path: str | Path) -> Path:
        """Resolve a user path safely within the workspace root."""

    @abstractmethod
    def read_text(self, path: str | Path, *, encoding: str = "utf-8") -> str:
        """Read text from a workspace-relative file."""

    @abstractmethod
    def write_text(self, path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
        """Write text to a workspace-relative file."""

    @abstractmethod
    async def run_command(
        self,
        command: str,
        *,
        timeout_seconds: int = 30,
    ) -> CommandResult:
        """Run one shell command inside the workspace."""

    @abstractmethod
    def to_sandbox_runtime_config(self) -> dict[str, object]:
        """Return an Anthropic sandbox-runtime config derived from this workspace."""


class LocalWorkspace(Workspace):
    """Local filesystem workspace rooted at one directory."""

    _MAX_COMMAND_OUTPUT_CHARS = 20_000

    def __init__(
        self,
        root_dir: str | Path,
        *,
        extra_read_paths: Sequence[str | Path] = (),
        extra_write_paths: Sequence[str | Path] = (),
        deny_write_paths: Sequence[str | Path] = (),
        allowed_domains: Sequence[str] = (),
        denied_domains: Sequence[str] = (),
        allow_unix_sockets: Sequence[str | Path] = (),
        allow_local_binding: bool = False,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self._root_dir = Path(root_dir).expanduser().resolve()
        self._extra_read_paths = tuple(extra_read_paths)
        self._extra_write_paths = tuple(extra_write_paths)
        self._deny_write_paths = tuple(deny_write_paths)
        self._allowed_domains = tuple(allowed_domains)
        self._denied_domains = tuple(denied_domains)
        self._allow_unix_sockets = tuple(allow_unix_sockets)
        self._allow_local_binding = allow_local_binding
        self._extra_env = dict(extra_env) if extra_env else {}

    @property
    def root_dir(self) -> Path:
        """Return the resolved workspace root directory."""
        return self._root_dir

    def resolve_path(self, path: str | Path) -> Path:
        """Resolve a path inside the workspace root."""
        candidate = (self.root_dir / Path(path)).resolve()
        candidate.relative_to(self.root_dir)
        return candidate

    def read_text(self, path: str | Path, *, encoding: str = "utf-8") -> str:
        """Read a text file from the local workspace."""
        return self.resolve_path(path).read_text(encoding=encoding)

    def write_text(self, path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
        """Write a text file into the local workspace."""
        target = self.resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)

    async def run_command(
        self,
        command: str,
        *,
        timeout_seconds: int = 30,
    ) -> CommandResult:
        """Run one shell command inside an ``srt`` sandbox over the workspace.

        Fails closed if ``srt`` is not installed: there is no fallback to
        unsandboxed execution. Env is rebuilt from a small allowlist (PATH,
        HOME, locale, plus operator ``extra_env``) so parent-process secrets
        like AWS_*/OPENAI_API_KEY/GH_TOKEN don't leak into the child.

        Note on argv: srt does not honour POSIX ``--`` as an end-of-options
        marker. Passing ``"--"`` between flags and the command silently
        breaks argv handling (the first token runs, the rest get dropped).
        Keep the command tokens directly after the last flag.
        """
        ensure_srt_available()
        settings_path = self._write_sandbox_settings_file()
        try:
            process = await asyncio.create_subprocess_exec(
                "srt",
                "--settings",
                settings_path,
                "sh",
                "-c",
                command,
                cwd=str(self.root_dir),
                env=_build_sandbox_env(self._root_dir, self._extra_env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                await _terminate_process_group(process)
                raise TimeoutError(
                    f"Command timed out after {timeout_seconds} seconds: {command}"
                ) from error
        finally:
            try:
                os.unlink(settings_path)
            except OSError:
                pass

        stdout, stdout_truncated = self._truncate_command_output(
            stdout_bytes.decode("utf-8", errors="replace")
        )
        stderr, stderr_truncated = self._truncate_command_output(
            stderr_bytes.decode("utf-8", errors="replace")
        )
        return CommandResult(
            command=command,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def _write_sandbox_settings_file(self) -> str:
        """Persist the sandbox config to a 0o600 JSON file. Caller must unlink."""
        fd, path = tempfile.mkstemp(suffix=".json", prefix="fortify-srt-", text=True)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(self.to_sandbox_runtime_config(), handle)
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def to_sandbox_runtime_config(self) -> dict[str, object]:
        """Return a sandbox-runtime config derived from this local workspace."""
        return build_sandbox_runtime_config(
            self.root_dir,
            extra_read_paths=self._extra_read_paths,
            extra_write_paths=self._extra_write_paths,
            deny_write_paths=self._deny_write_paths,
            allowed_domains=self._allowed_domains,
            denied_domains=self._denied_domains,
            allow_unix_sockets=self._allow_unix_sockets,
            allow_local_binding=self._allow_local_binding,
        )

    def _truncate_command_output(self, output: str) -> tuple[str, bool]:
        """Bound command output size so tool payloads stay manageable."""
        if len(output) <= self._MAX_COMMAND_OUTPUT_CHARS:
            return output, False
        return output[: self._MAX_COMMAND_OUTPUT_CHARS], True
