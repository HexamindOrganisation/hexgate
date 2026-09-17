"""Top-down sub-agent enumeration — read a parent's sub-agent edges per framework.

`enumerate_subagents(agent)` returns the agent's outgoing sub-agent edges as a
list of :class:`SubagentLink` (``target`` name + ``via`` + an optional live
``child`` handle). It reads whatever the framework exposes:

- **HexgateAgent** — the ``subagents=`` registry authored in PR 1 (native/pydantic).
- **OpenAI Agents** — ``Agent.handoffs`` (handoff) and ``Agent.as_tool()`` origin
  metadata (tool), via the adapter's existing origin reader.
- **Google ADK** — ``sub_agents`` (handoff) and ``AgentTool`` on ``tools`` (tool).
- **Everything else** (a raw LangGraph graph, a Pydantic AI agent, an unknown
  object) — the sub-agent hides in a tool closure with no handle, so ``[]``.

This is pure introspection: no rewrapping, no policy, no behavior change. It is
the reader that PR 3 (manifest refs) and PR 4 (recursive registration) build on.

``SubagentLink`` is intentionally distinct from the authoring ``SubagentEdge``:
enumeration must describe an edge the framework may only expose *by name* (an
OpenAI ``as_tool`` origin carries the target name, not the agent object), so
``target`` is always present and ``child`` is the live object only when there is
one to recurse into.

Framework dispatch mirrors :func:`hexgate.manifest.builder.create_manifest` — the
same module-string classifier — so enumeration only ever reports edges on an agent
that ``create_manifest`` can also build a manifest for (PR 4 needs both).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hexgate.security.models import AgentVia
from hexgate.security.naming import canonical_agent_name, canonical_name


@dataclass(frozen=True)
class SubagentLink:
    """One outgoing sub-agent edge discovered on a parent agent.

    ``target`` is the canonical target-agent name (the ``agent.<via>:<target>``
    reach key's target); ``via`` is the edge kind; ``child`` is the live target
    object when the framework exposes one (to recurse into), else ``None``.

    ``child`` is excluded from equality/hash (``compare=False``): a framework agent
    (OpenAI ``Agent``, Google ``LlmAgent``) is unhashable, and a link's identity is
    its ``(target, via)`` — so ``set(enumerate_subagents(...))`` dedups by edge.
    """

    target: str
    via: AgentVia
    child: Any = field(default=None, compare=False)


def enumerate_subagents(agent: object) -> list[SubagentLink]:
    """Return ``agent``'s outgoing sub-agent edges (empty when none are visible)."""
    from hexgate.agents.factory import HexgateAgent

    if isinstance(agent, HexgateAgent):
        # canonical_agent_name (not canonical_name(child.name)) — the same derivation
        # the gate uses at every seam, and it defaults a missing name to 'default'
        # rather than raising AttributeError on a handoff child that isn't a HexgateAgent.
        return [
            SubagentLink(canonical_agent_name(edge.child), edge.via, edge.child)
            for edge in agent.subagents
        ]

    module = type(agent).__module__
    if module == "agents" or module.startswith("agents."):
        return _openai_links(agent)
    if module.startswith("google.adk"):
        return _google_links(agent)
    # Raw LangGraph graph, Pydantic AI, or unknown: the child is a tool closure
    # with no handle — nothing to enumerate.
    return []


def _openai_links(agent: object) -> list[SubagentLink]:
    """OpenAI Agents: ``handoffs`` (handoff) + ``as_tool()`` origins (tool)."""
    try:
        from agents import Agent, FunctionTool

        from hexgate.adapters.openai.tools import _agent_tool_target
    except ImportError:
        # The module name looked like OpenAI's (a foreign object in a locally-named
        # ``agents`` package), but the SDK isn't installed — honor the ``[]`` contract.
        return []

    links: list[SubagentLink] = []
    for handoff in getattr(agent, "handoffs", None) or []:
        if isinstance(handoff, Agent):
            # A live ``Agent`` handoff target — recursable; canonical maps '' -> 'default',
            # matching the gate (canonical_agent_name at the on_handoff seam). An isinstance
            # check, not an attribute sniff, so a ``Handoff`` that grows a ``name`` alias
            # in a future SDK isn't misread as an Agent.
            links.append(
                SubagentLink(canonical_agent_name(handoff), "handoff", handoff)
            )
            continue
        # A bare ``Handoff`` descriptor — only ``agent_name``; the live target is
        # behind a weakref (``_agent_ref``) when still alive, else unrecoverable.
        agent_name = getattr(handoff, "agent_name", None)
        if agent_name is not None:
            ref = getattr(handoff, "_agent_ref", None)
            child = ref() if callable(ref) else None
            links.append(SubagentLink(canonical_name(agent_name), "handoff", child))
    for tool in getattr(agent, "tools", None) or []:
        if not isinstance(tool, FunctionTool):
            continue
        # The shared origin reader returns a canonical name, or ``None`` for a plain
        # tool (and for a nameless origin — matching what the gate treats as reach).
        target = _agent_tool_target(tool)
        if target:
            # The live sub-agent handle lives on ``FunctionTool._agent_instance``; the
            # as_tool origin metadata carries only ``agent_name``, not the object.
            links.append(
                SubagentLink(target, "tool", getattr(tool, "_agent_instance", None))
            )
    return links


def _google_links(agent: object) -> list[SubagentLink]:
    """Google ADK: ``sub_agents`` (handoff) + ``AgentTool`` on ``tools`` (tool)."""
    try:
        from google.adk.tools.agent_tool import AgentTool

        from hexgate.adapters.google.tools import _agent_tool_target
    except ImportError:
        return []

    links: list[SubagentLink] = []
    for sub in getattr(agent, "sub_agents", None) or []:
        # canonical maps '' -> 'default', matching the transfer_to_agent seam.
        links.append(SubagentLink(canonical_agent_name(sub), "handoff", sub))
    for tool in getattr(agent, "tools", None) or []:
        if not isinstance(tool, AgentTool):
            continue
        target = _agent_tool_target(
            tool
        )  # canonical name, or None for a nameless agent
        if target:
            links.append(SubagentLink(target, "tool", tool.agent))
    return links


__all__ = ["SubagentLink", "enumerate_subagents"]
