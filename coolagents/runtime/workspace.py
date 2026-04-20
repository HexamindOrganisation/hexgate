"""Workspace abstractions for runtime-scoped file access."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from coolagents.runtime.sandbox_runtime import build_sandbox_runtime_config


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
    def to_sandbox_runtime_config(self) -> dict[str, object]:
        """Return an Anthropic sandbox-runtime config derived from this workspace."""


class LocalWorkspace(Workspace):
    """Local filesystem workspace rooted at one directory."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        extra_read_paths: Sequence[str | Path] = (),
        extra_write_paths: Sequence[str | Path] = (),
        deny_write_paths: Sequence[str | Path] = (),
        allowed_domains: Sequence[str] = (),
        denied_domains: Sequence[str] = (),
    ) -> None:
        self._root_dir = Path(root_dir).expanduser().resolve()
        self._extra_read_paths = tuple(extra_read_paths)
        self._extra_write_paths = tuple(extra_write_paths)
        self._deny_write_paths = tuple(deny_write_paths)
        self._allowed_domains = tuple(allowed_domains)
        self._denied_domains = tuple(denied_domains)

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

    def to_sandbox_runtime_config(self) -> dict[str, object]:
        """Return a sandbox-runtime config derived from this local workspace."""
        return build_sandbox_runtime_config(
            self.root_dir,
            extra_read_paths=self._extra_read_paths,
            extra_write_paths=self._extra_write_paths,
            deny_write_paths=self._deny_write_paths,
            allowed_domains=self._allowed_domains,
            denied_domains=self._denied_domains,
        )
