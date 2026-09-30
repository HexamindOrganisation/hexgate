"""deepagents skill activation gating for the in-place LangChain installer.

deepagents has no skill tool: the model activates a skill by calling ``read_file``
on the ``SKILL.md`` path its system prompt lists, verbatim. :class:`SkillKeyResolver`
recognises reads of known skill files and redirects their decision to the
``skill:`` / ``skill.resource:`` key. Paths are indexed at wrap time, so a skill
added to a source after wrapping, or served from per-run state, is not gated.
"""

from __future__ import annotations

import posixpath
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from hexgate.guards.runner import PolicyOverride, RenderError
from hexgate.manifest.langchain import SkillLocation
from hexgate.security.decision import Decision
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.models import SkillVia, skill_key
from hexgate.security.skill_gate import skill_decision_hash, skill_denial_message

_FILE_READ_TOOL = "read_file"
_FILE_PATH_ARG = "file_path"
_POSIX_SEP = "/"
_WINDOWS_SEP = "\\"

SkillHasher = Callable[[SkillLocation], str | None]
AsyncSkillHasher = Callable[[SkillLocation], Awaitable[str | None]]


def canonical_skill_path(path: str) -> str:
    """The path deepagents' ``validate_path`` would read, for index lookup.

    Mirrors its rewrites (backslashes to slashes, ``normpath``, a leading slash) so
    ``skills/x/SKILL.md`` and ``\\skills\\x\\SKILL.md`` match ``/skills/x/SKILL.md``.
    It also collapses ``..``, which deepagents rejects; matching more spellings
    than it reads only gates more, never less.
    """
    normalized = posixpath.normpath(path.replace(_WINDOWS_SEP, _POSIX_SEP))
    if not normalized.startswith(_POSIX_SEP):
        normalized = f"{_POSIX_SEP}{normalized}"
    return posixpath.normpath(normalized)


@dataclass(frozen=True)
class SkillPathIndex:
    """Known skill files: exact ``SKILL.md`` paths, and each skill's directory."""

    by_skill_md: Mapping[str, SkillLocation]
    by_dir: Mapping[str, SkillLocation]

    @classmethod
    def from_locations(cls, locations: list[SkillLocation]) -> SkillPathIndex:
        by_skill_md: dict[str, SkillLocation] = {}
        by_dir: dict[str, SkillLocation] = {}
        for location in locations:
            skill_md = canonical_skill_path(location.skill_md_path)
            by_skill_md[skill_md] = location
            by_dir[str(PurePosixPath(skill_md).parent)] = location
        return cls(by_skill_md, by_dir)

    def __bool__(self) -> bool:
        return bool(self.by_skill_md)

    def match(self, path: str) -> tuple[SkillVia, SkillLocation] | None:
        """The level and skill a read of ``path`` reaches, or None if it is no skill's.

        A ``SKILL.md`` itself is the instructions; any file below a skill's
        directory, however deep, is a resource of the nearest enclosing skill.
        """
        normalized = canonical_skill_path(path)
        location = self.by_skill_md.get(normalized)
        if location is not None:
            return "instructions", location
        for ancestor in PurePosixPath(normalized).parents:
            location = self.by_dir.get(str(ancestor))
            if location is not None:
                return "resource", location
        return None


def _render_skill_error(skill: str, via: SkillVia) -> RenderError:
    """Model-facing renderer for a denied/held skill read, as a LangChain error dict."""

    def render(decision: Decision) -> dict[str, Any]:
        payload = decision.as_error_payload()
        payload["message"] = skill_denial_message(skill, via, decision)
        return {"ok": False, "error": payload}

    return render


class SkillKeyResolver:
    """Resolve a ``read_file`` of a known skill file to its skill policy key.

    Engagement is read per call, so a hot-reloaded policy that starts declaring
    skills engages the gate without re-wrapping. The content hash is read per call,
    just before the tool reads the file itself: a pin checks the body as it stands
    at decision time, not at wrap time. It is a separate read, so a writable source
    changed between the two can still slip past a pin.
    """

    def __init__(
        self,
        enforcer: PolicyEnforcer,
        index: SkillPathIndex,
        hasher: SkillHasher,
        ahasher: AsyncSkillHasher,
    ) -> None:
        self._enforcer = enforcer
        self._index = index
        self._hasher = hasher
        self._ahasher = ahasher

    def resolve(self, tool_name: str, args: Mapping[str, Any]) -> PolicyOverride | None:
        read = self._match(tool_name, args)
        if read is None:
            return None
        return read.override(self._hasher(read.location))

    async def aresolve(
        self, tool_name: str, args: Mapping[str, Any]
    ) -> PolicyOverride | None:
        read = self._match(tool_name, args)
        if read is None:
            return None
        return read.override(await self._ahasher(read.location))

    def _match(self, tool_name: str, args: Mapping[str, Any]) -> _SkillRead | None:
        if tool_name != _FILE_READ_TOOL:
            return None
        path = args.get(_FILE_PATH_ARG)
        if not isinstance(path, str):
            return None
        if not self._enforcer.policy.declares_skills():
            return None
        matched = self._index.match(path)
        if matched is None:
            return None
        return _SkillRead(*matched, canonical_skill_path(path))


@dataclass(frozen=True)
class _SkillRead:
    via: SkillVia
    location: SkillLocation
    path: str

    def override(self, digest: str | None) -> PolicyOverride:
        """Decision args carry the canonical path, so constraints see one spelling."""
        name = self.location.name
        return PolicyOverride(
            key=skill_key(self.via, name),
            args={
                "skill": name,
                "via": self.via,
                _FILE_PATH_ARG: self.path,
                "content_hash": skill_decision_hash(digest),
            },
            render_error=_render_skill_error(name, self.via),
        )
