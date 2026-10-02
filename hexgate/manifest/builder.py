from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hexgate.agents.enumeration import enumerate_subagents
from hexgate.agents.factory import HexgateAgent
from hexgate.guards.attach import resolve_guards
from hexgate.manifest.models import (
    MAX_GUARDS,
    AgentManifest,
    AgentType,
    GuardManifest,
    SubagentRef,
    _truncate,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from langchain_core.tools import BaseTool

    from hexgate.guards.types import Guard

_log = logging.getLogger(__name__)


def create_manifest(
    agent: AgentType,
    *,
    description: str | None = None,
    tools: list[BaseTool] | None = None,
    model: object | None = None,
    system_prompt: object | None = None,
    skills_middleware: object | None = None,
    guards: Sequence[Guard] | None = None,
) -> AgentManifest:
    """Create an AgentManifest from an Agent.

    `tools` is required and used explicitly only when `agent` is a raw LangChain
    compiled graph, since those graphs do not reliably expose their tool nodes.
    The same is true of `model` and `system_prompt`. A graph's deepagents skills
    are discovered from its compiled ``SkillsMiddleware``; `skills_middleware`
    overrides that instance. Other frameworks ignore it so callers can pass a
    uniform kwarg set.

    Framework-specific submodules (and their SDK imports) are loaded lazily so
    callers only import the SDK they actually use.

    Sub-agent reach edges are enumerated (:func:`enumerate_subagents`) and recorded
    on ``manifest.subagents`` so registration and the dashboard can see the agent
    graph. Kept ``None`` when there are none, for content-hash continuity.

    ``guards`` is the flat guard list the agent was built with. Normally it is
    read off the agent — :func:`~hexgate.guards.attach_guards` stamps it there at
    construction (``create_agent(guards=...)`` does so automatically), so the CLI,
    which only holds the loaded agent object, still surfaces guards. An explicit
    ``guards=`` overrides the stamp (a programmatic caller, or a raw LangGraph with
    nothing stamped). Guards are framework-agnostic, so they attach after the
    dispatch. Kept ``None`` when there are none, for content-hash continuity.
    """
    manifest = _build_base_manifest(
        agent,
        description=description,
        tools=tools,
        model=model,
        system_prompt=system_prompt,
        skills_middleware=skills_middleware,
    )
    refs = [
        SubagentRef(name=link.target, via=link.via)
        for link in enumerate_subagents(agent)
    ]
    if refs:
        manifest.subagents = refs
    resolved_guards = resolve_guards(agent, guards)
    if resolved_guards:
        # Cap here, not via a field_validator: guards are assigned post-construction
        # (framework-agnostic, so they attach after the per-framework dispatch), and
        # Pydantic does not validate assignment by default — a validator would never
        # run. Truncate the source list, mirroring the skills bound.
        capped = _truncate(list(resolved_guards), MAX_GUARDS, "guards")
        manifest.guards = [_to_guard_manifest(g) for g in capped]
    return manifest


def _to_guard_manifest(guard: Guard) -> GuardManifest:
    """Map a runtime :class:`~hexgate.guards.types.Guard` to its manifest form.

    Framework-agnostic: a guard is the same object however the agent was built.
    ``official`` is a display hint — a guard whose function lives in
    ``hexgate.plugins`` is a built-in; for those the label is the plugin id.
    """
    name = guard.label
    if not name.isidentifier():
        # The platform governs a guard by its name; an inline lambda (label
        # '<lambda>') or a repr fallback has no stable, addressable one, so it
        # cannot be enabled/disabled and two would collide. Report it faithfully,
        # but warn — a governed guard should be a named function.
        _log.warning(
            "guard %r has no stable name (e.g. an inline lambda); the platform "
            "cannot address it to enable/disable it — give it a named function.",
            name,
        )
    official = _is_official_guard(guard.fn)
    return GuardManifest(
        name=name,
        position="before" if guard.position == "pre" else "after",
        tool_names=sorted(guard.tool_names) if guard.tool_names is not None else None,
        observe=guard.observe,
        kind="official" if official else "custom",
        plugin_id=name if official else None,
    )


def _is_official_guard(fn: Callable[..., object]) -> bool:
    """True when the guard function ships in the ``hexgate.plugins`` package.

    Matches the package itself or a dotted submodule of it — not a bare string
    prefix, so a caller's own ``hexgate.plugins_local`` is not mis-stamped as a
    built-in. ``__module__`` may be absent or ``None`` (a C/builtin callable),
    which counts as not-official rather than crashing.
    """
    mod = getattr(fn, "__module__", None) or ""
    return mod == "hexgate.plugins" or mod.startswith("hexgate.plugins.")


def _build_base_manifest(
    agent: AgentType,
    *,
    description: str | None,
    tools: list[BaseTool] | None,
    model: object | None,
    system_prompt: object | None,
    skills_middleware: object | None,
) -> AgentManifest:
    """Dispatch to the framework-specific manifest builder (no sub-agents yet)."""
    if isinstance(agent, HexgateAgent):
        from hexgate.manifest.native import create_hexgate_manifest

        return create_hexgate_manifest(agent, description=description)

    module = type(agent).__module__
    if module == "agents" or module.startswith("agents."):
        from hexgate.manifest.openai import create_openai_manifest

        return create_openai_manifest(agent, description=description)

    if module.startswith("google.adk"):
        from hexgate.manifest.google import create_google_manifest

        return create_google_manifest(agent, description=description)

    if module == "langgraph" or module.startswith("langgraph."):
        from hexgate.manifest.langchain import create_langchain_manifest

        if tools is None:
            raise ValueError(
                "LangChain graphs require `tools` to be passed explicitly to create_manifest()"
            )
        return create_langchain_manifest(
            agent,
            tools,
            description=description,
            model=model,
            system_prompt=system_prompt,
            skills_middleware=skills_middleware,
        )

    if module == "pydantic_ai" or module.startswith("pydantic_ai."):
        from hexgate.manifest.pydantic_ai import create_pydantic_ai_manifest

        return create_pydantic_ai_manifest(agent, description=description)

    raise ValueError(f"Unsupported agent type: {type(agent)}")
