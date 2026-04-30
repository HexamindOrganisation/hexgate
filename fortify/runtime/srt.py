"""Detection helpers for the Anthropic sandbox-runtime (``srt``) binary.

The bash tool relies on ``srt`` to enforce filesystem and network policy
on shell commands. We treat its presence as a hard runtime requirement:
if ``srt`` is missing, ``run_command`` must fail closed rather than
silently fall back to unsandboxed execution.

This module is a thin probe layer with no side effects beyond reading
PATH and executing ``srt --version``. Wiring into the workspace happens
in a later phase.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

_INSTALL_HINT = (
    "Install with: npm install -g @anthropic-ai/sandbox-runtime\n"
    "Repository:   https://github.com/anthropic-experimental/sandbox-runtime"
)

_SRT_BINARY = "srt"
_VERSION_PROBE_TIMEOUT_SECONDS = 5


class SrtUnavailableError(RuntimeError):
    """Raised when ``srt`` is not installed or the platform isn't supported."""


def find_srt() -> str:
    """Return the absolute path to the ``srt`` binary.

    Raises ``SrtUnavailableError`` if ``srt`` is not on ``PATH`` or the
    current platform is unsupported. The error message includes install
    instructions so operators get a usable hint at the failure site.
    """
    if sys.platform == "win32":
        raise SrtUnavailableError(
            "sandbox-runtime does not support Windows. "
            "Run the agent on macOS or Linux."
        )
    path = shutil.which(_SRT_BINARY)
    if path is None:
        raise SrtUnavailableError(
            f"sandbox-runtime binary {_SRT_BINARY!r} not found on PATH.\n"
            f"{_INSTALL_HINT}"
        )
    return path


def ensure_srt_available() -> None:
    """Verify ``srt`` is installed and usable; raise on failure.

    Convenience wrapper for callers that don't need the resolved path.
    """
    find_srt()


def srt_version() -> str | None:
    """Return the installed ``srt`` version string, or ``None`` if unknown.

    Best-effort probe intended for logs and diagnostics, not control flow.
    A missing binary, exec error, timeout, or non-zero exit all map to
    ``None``. Output is stripped; if both stdout and stderr are empty the
    function returns ``None`` rather than an empty string.
    """
    try:
        path = find_srt()
    except SrtUnavailableError:
        return None

    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    output = (result.stdout or result.stderr or "").strip()
    return output or None
