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
) -> dict[str, object]:
    """Build an Anthropic sandbox-runtime config from workspace intent."""
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
        },
    }
