"""Helpers for deriving Anthropic sandbox-runtime config from a workspace."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def _resolve_runtime_path(workspace_root: Path, path: str | Path) -> Path:
    """Resolve sandbox config paths relative to the workspace root when needed."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return candidate.resolve()


def _resolve_socket_path(workspace_root: Path, path: str | Path) -> Path:
    """Resolve a Unix-socket path without following symlinks.

    Sockets like ``/var/run/docker.sock`` are commonly symlinked (e.g. to
    ``$HOME/.docker/run/docker.sock`` on macOS). The operator's literal is
    what programs pass to ``connect()``; canonicalizing here would cause
    the sandbox filter to miss against the user-space syscall argument.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return candidate


def _normalize_domain_list(domains: Sequence[str]) -> list[str]:
    """Return unique domain entries while preserving order."""
    seen: set[str] = set()
    normalized: list[str] = []
    for domain in domains:
        value = domain.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _normalize_path_list(paths: Sequence[Path]) -> list[str]:
    """Return unique absolute path strings while preserving order."""
    seen: set[str] = set()
    normalized: list[str] = []
    for path in paths:
        value = str(path)
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def default_deny_read_paths(workspace_root: Path) -> list[Path]:
    """Return conservative host paths to deny before reopening the workspace."""
    home_dir = Path.home().expanduser().resolve()
    deny_paths = [home_dir]

    # Never emit a blanket root deny; it is too aggressive for normal process startup.
    _ = workspace_root
    return deny_paths


def build_sandbox_runtime_config(
    workspace_root: str | Path,
    *,
    extra_read_paths: Sequence[str | Path] = (),
    extra_write_paths: Sequence[str | Path] = (),
    deny_write_paths: Sequence[str | Path] = (),
    allowed_domains: Sequence[str] = (),
    denied_domains: Sequence[str] = (),
    allow_unix_sockets: Sequence[str | Path] = (),
    allow_local_binding: bool = False,
) -> dict[str, object]:
    """Build an Anthropic sandbox-runtime config from workspace intent.

    ``allow_unix_sockets`` lists Unix-domain socket paths that the sandbox
    should permit (e.g. ``/var/run/docker.sock``); on Linux ``srt`` blocks
    ``AF_UNIX`` socket creation by default. ``allow_local_binding`` opts
    into letting sandboxed processes bind to localhost ports. Both default
    to the most restrictive value so widening the boundary is opt-in.
    """
    resolved_root = Path(workspace_root).expanduser().resolve()
    resolved_extra_reads = [
        _resolve_runtime_path(resolved_root, path) for path in extra_read_paths
    ]
    resolved_extra_writes = [
        _resolve_runtime_path(resolved_root, path) for path in extra_write_paths
    ]
    resolved_deny_writes = [
        _resolve_runtime_path(resolved_root, path) for path in deny_write_paths
    ]
    resolved_unix_sockets = [
        _resolve_socket_path(resolved_root, path) for path in allow_unix_sockets
    ]

    return {
        "filesystem": {
            "denyRead": _normalize_path_list(default_deny_read_paths(resolved_root)),
            "allowRead": _normalize_path_list([resolved_root, *resolved_extra_reads]),
            "allowWrite": _normalize_path_list(
                [resolved_root, Path("/tmp"), *resolved_extra_writes]
            ),
            "denyWrite": _normalize_path_list(resolved_deny_writes),
        },
        "network": {
            "allowedDomains": _normalize_domain_list(allowed_domains),
            "deniedDomains": _normalize_domain_list(denied_domains),
            "allowUnixSockets": _normalize_path_list(resolved_unix_sockets),
            "allowLocalBinding": allow_local_binding,
        },
    }
