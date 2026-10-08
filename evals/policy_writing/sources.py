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
    """agents.json or audit.json is malformed, or agents.json is missing."""


def _reading[**P, R](load: Callable[P, R]) -> Callable[P, R]:
    """`load`, with anything a malformed file raises turned into a SourceError."""

    @wraps(load)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return load(*args, **kwargs)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SourceError(repr(exc)) from exc

    return wrapper


def _views(ws: Path) -> list[dict]:
    """agents.json's `AgentManifestView`s, read as the endpoint returns them
    rather than as the SDK registers them: the view is looser (a tool's
    `description` may be null)."""
    return json.loads((ws / AGENTS_JSON).read_text())


@_reading
def load_attributes(ws: Path, agent: str) -> set[str]:
    """The caller attributes (`ctx.*`) `agent` is known to send.

    `audit.json` stands in for the Hexgate MCP's `audit_decisions`:
    `AuditDecisionRow`s, as a list or the endpoint's page (`{rows, ...}`).
    Attributes are set per request and are in no manifest, so the known ones are
    the `attributes` keys of `agent`'s rows. No `audit.json` means none."""
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
    as the SDK's `AgentManifest`.

    `agents.json` stands in for the Hexgate MCP's `agents_list`
    (`GET /projects/{id}/agents/manifest`, a list of `AgentManifestView`)."""
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
    required: the policy checks never read it, so a null becomes empty. Null
    `skills` become none: the SDK reads null as unknown (a listing that may have
    failed), but the MCP shows this agent no skills."""
    tools = [
        {**t, "description": t.get("description") or ""} for t in manifest["tools"]
    ]
    skills = manifest.get("skills") or []
    return AgentManifest.model_validate({**manifest, "tools": tools, "skills": skills})
