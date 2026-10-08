"""The starting project's stand-ins for the Hexgate MCP, read: `agents.json`
(manifests) and `audit.json` (audit rows, the only source of caller-attribute
names)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from hexgate.manifest.models import AgentManifest

# The files the names come from.
AGENTS_JSON, AUDIT_JSON = NAME_SOURCES = ("agents.json", "audit.json")


class SourceError(ValueError):
    """agents.json or audit.json is missing, malformed, or lacks the case agent."""


def _reading[**P, R](load: Callable[P, R]) -> Callable[P, R]:
    """`load`, with anything a malformed file raises turned into a SourceError."""

    @wraps(load)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return load(*args, **kwargs)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SourceError(repr(exc)) from exc

    return wrapper


@dataclass(frozen=True)
class KnownNames:
    tools: dict[str, set[str]]  # tool name: its argument names
    attrs: set[str]  # caller attributes (`ctx.*`)
    skills: set[str]
    guards: set[str]


@_reading
def load_known_names(ws: Path, agent: str) -> KnownNames:
    """The names a policy for `agent` may use.

    The starting project stands in for what the Hexgate MCP's tools (per its
    design) return:
    - `agents.json` is `agents_list` (`GET /projects/{id}/agents/manifest`, a
      list of `AgentManifestView`). Tools with their argument names, skills and
      guards come from `agent`'s manifest only, so a tool another agent in the
      project has is unknown.
    - `audit.json` is `audit_decisions`: `AuditDecisionRow`s, as a list or the
      endpoint's page (`{rows, ...}`). Caller attributes are set per request and
      are not in the manifest, so the known ones are the `attributes` keys of
      `agent`'s rows. No `audit.json` means no known attributes.
    """
    manifest = _manifest(ws, agent)
    return KnownNames(**_names([manifest]), attrs=_attributes(ws, agent))


@_reading
def load_project_names(ws: Path, agent: str) -> KnownNames:
    """`load_known_names`, but with every agent's tools, skills and guards: a
    module tree's `"*"` cells and boundaries may name any agent's, and
    `check_project` checks `agent`'s named column against its manifest. Caller
    attributes stay `agent`'s own: it is the one sending them."""
    _manifest(ws, agent)  # the case agent must have one, as for load_known_names
    manifests = [v["manifest"] for v in _views(ws) if v.get("manifest") is not None]
    return KnownNames(**_names(manifests), attrs=_attributes(ws, agent))


def _names(manifests: list[dict]) -> dict:
    """The tools (with their argument names), skills and guards `manifests`
    list between them."""
    tools: dict[str, set[str]] = {}
    for m in manifests:
        for t in m["tools"]:
            tools.setdefault(t["name"], set()).update(t["input_schema"]["properties"])
    return {
        "tools": tools,
        # The endpoint sends `null` for an agent with none.
        "skills": {s["name"] for m in manifests for s in m.get("skills") or []},
        "guards": {g["name"] for m in manifests for g in m.get("guards") or []},
    }


def _views(ws: Path) -> list[dict]:
    """agents.json's `AgentManifestView`s, read as the endpoint returns them
    rather than as the SDK registers them: the view is looser (a tool's
    `description` may be null)."""
    return json.loads((ws / AGENTS_JSON).read_text())


def _manifest(ws: Path, agent: str) -> dict:
    """`agent`'s manifest from agents.json, in the endpoint's shape."""
    view = next(
        (v for v in _views(ws) if v["name"] == agent and v.get("manifest") is not None),
        None,
    )
    if view is None:
        raise ValueError(f"agents.json has no manifest for agent {agent!r}")
    return view["manifest"]


def _attributes(ws: Path, agent: str) -> set[str]:
    """The `attributes` keys of `agent`'s audit.json rows."""
    audit = ws / AUDIT_JSON
    rows = json.loads(audit.read_text()) if audit.exists() else []
    if isinstance(rows, dict):  # the endpoint's page shape, {rows, total, ...}
        rows = rows["rows"]
    return {
        name
        for row in rows
        if row["agent_name"] == agent
        for name in row.get("attributes") or {}
    }


@dataclass(frozen=True)
class ProjectAgents:
    """The project's agents, as `check_project` takes them."""

    registered: frozenset[str]  # every agent, with a manifest or not
    manifests: dict[str, AgentManifest]  # the agents with one


@_reading
def load_project_agents(ws: Path) -> ProjectAgents:
    """Every agent in agents.json, and the manifests of those registered with one,
    as the SDK's `AgentManifest` (as the platform's `latest_manifests` builds them
    for the policy checks)."""
    views = _views(ws)
    return ProjectAgents(
        registered=frozenset(v["name"] for v in views),
        manifests={
            v["name"]: _sdk_manifest(v["manifest"])
            for v in views
            if v.get("manifest") is not None
        },
    )


def _sdk_manifest(manifest: dict) -> AgentManifest:
    """The endpoint's manifest as the SDK's model, whose tool `description` is
    required: the policy checks never read it, so a null becomes empty."""
    tools = [
        {**t, "description": t.get("description") or ""} for t in manifest["tools"]
    ]
    return AgentManifest.model_validate({**manifest, "tools": tools})
