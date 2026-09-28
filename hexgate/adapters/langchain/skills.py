"""deepagents skill activation gating for the in-place LangChain installer.

deepagents has no skill tool: the model activates a skill by calling ``read_file``
on the ``SKILL.md`` path its system prompt lists, verbatim. :class:`SkillKeyResolver`
recognises reads of known skill files and redirects their decision to the
``skill:`` / ``skill.resource:`` key. Paths are indexed at wrap time, so a skill
added to a source after wrapping, or served from per-run state, is not gated.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from hexgate.adapters.langchain.tools import PolicyOverride
from hexgate.guards.runner import RenderError
from hexgate.manifest.langchain import SkillLocation
from hexgate.security.decision import Decision, DecisionOutcome
from hexgate.security.enforcer import PolicyEnforcer
from hexgate.security.models import SkillVia, skill_key

_FILE_READ_TOOL = "read_file"
_FILE_PATH_ARG = "file_path"
# Matches the ADK adapter's decision args, so one pin reads alike on both.
_CONTENT_HASH_PREFIX = "sha256:"

_SKILL_HELD_ACTION_BY_VIA: dict[SkillVia, str] = {
    "instructions": "before it is loaded",
    "resource": "before its resource is read",
}

SkillHasher = Callable[[SkillLocation], str | None]


def _normalize(path: str) -> str:
    """Collapse ``..`` and duplicate separators so a respelled path still matches."""
    return posixpath.normpath(path)


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
            skill_md = _normalize(location.skill_md_path)
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
        normalized = _normalize(path)
        location = self.by_skill_md.get(normalized)
        if location is not None:
            return "instructions", location
        for ancestor in PurePosixPath(normalized).parents:
            location = self.by_dir.get(str(ancestor))
            if location is not None:
                return "resource", location
        return None


def _render_skill_error(skill: str, via: SkillVia) -> RenderError:
    """Model-facing renderer for a denied/held skill read.

    The closing sentence is deliberate: without it a model tends to improvise the
    procedure from memory, dropping exactly the guardrails the skill encoded."""

    def render(decision: Decision) -> dict[str, Any]:
        if decision.outcome is DecisionOutcome.NEEDS_APPROVAL:
            body = f"skill {skill!r} requires human approval {_SKILL_HELD_ACTION_BY_VIA[via]}"
        else:
            body = f"skill {skill!r} is not permitted by this agent's policy"
        payload = decision.as_error_payload()
        payload["message"] = f"{body}. Do not attempt this task without it."
        return {"ok": False, "error": payload}

    return render


class SkillKeyResolver:
    """Resolve a ``read_file`` of a known skill file to its skill policy key.

    Engagement is read per call, so a hot-reloaded policy that starts declaring
    skills engages the gate without re-wrapping. The content hash is read per call
    too, so a pin checks the body the model is about to read, not the one present
    at wrap time.
    """

    def __init__(
        self, enforcer: PolicyEnforcer, index: SkillPathIndex, hasher: SkillHasher
    ) -> None:
        self._enforcer = enforcer
        self._index = index
        self._hasher = hasher

    def __call__(
        self, tool_name: str, args: Mapping[str, Any]
    ) -> PolicyOverride | None:
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
        via, location = matched
        digest = self._hasher(location)
        return PolicyOverride(
            key=skill_key(via, location.name),
            args={
                "skill": location.name,
                "via": via,
                _FILE_PATH_ARG: path,
                "content_hash": f"{_CONTENT_HASH_PREFIX}{digest}" if digest else None,
            },
            render_error=_render_skill_error(location.name, via),
        )
