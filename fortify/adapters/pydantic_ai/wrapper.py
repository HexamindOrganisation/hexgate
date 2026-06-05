"""pydantic_ai adapter: build a :class:`PolicySet`, construct one
:class:`PolicyEnforcer`, and return a :class:`FortifyPydanticAgent`
proxy backed by a clone of the caller's ``Agent`` with policy-gated
tools.
"""

from __future__ import annotations

import copy
import os

from pydantic_ai import Agent
from pydantic_ai.tools import Tool

from fortify import audit
from fortify.adapters.pydantic_ai.agent import FortifyPydanticAgent
from fortify.adapters.pydantic_ai.tools import wrap_tools
from fortify.security import AgentPolicy, BaseToolPolicy, PolicySet
from fortify.security.enforcer import PolicyEnforcer
from fortify.security.policy_set import DEFAULT_ROLE_NAME


def build_policy_set(
    api_key: str,  # noqa: ARG001 — reserved for the future Fortify-cloud fetch
    agent_name: str,  # noqa: ARG001 — same
    tool_names: list[str],
) -> PolicySet:
    """Placeholder allow-all one-role bundle. TODO: cloud-fetch via FortifyClient."""
    default_policy = AgentPolicy(
        tools={name: BaseToolPolicy(mode="allow") for name in tool_names}
    )
    return PolicySet({DEFAULT_ROLE_NAME: default_policy})


def _extract_tools(agent: Agent) -> list[Tool]:
    """Return Tool instances from ``agent._function_toolset`` (constructor
    args and ``@agent.tool``/``tool_plain`` decorators normalize there)."""
    toolset = getattr(agent, "_function_toolset", None)
    tools = getattr(toolset, "tools", None) if toolset is not None else None
    if tools is None:
        return []
    return list(tools.values())


def _clone_agent_with_tools(agent: Agent, wrapped_tools: list[Tool]) -> Agent:
    """Return a shallow copy of ``agent`` with ``wrapped_tools`` installed."""
    agent_copy = copy.copy(agent)
    agent_copy.instrument = True
    toolset = getattr(agent, "_function_toolset", None)
    if toolset is not None:
        toolset_copy = copy.copy(toolset)
        toolset_copy.tools = {t.name: t for t in wrapped_tools}
        agent_copy._function_toolset = toolset_copy
    return agent_copy


def wrap_pydantic_agent(
    *,
    agent: Agent,
    api_key: str | None = None,
) -> FortifyPydanticAgent:
    """Wrap a pydantic_ai agent with Fortify policy + observability.

    Returns a :class:`FortifyPydanticAgent` backed by a clone of the
    caller's ``agent``; the original is not mutated. The proxy takes
    ``user`` per call; role resolves at call time from the active
    :class:`User`. ``NEEDS_APPROVAL`` raises :class:`ModelRetry` with
    an ``[approval_required]`` marker. ``api_key`` falls back to
    ``FORTIFY_KEY``.
    """
    resolved_key = api_key or os.getenv("FORTIFY_KEY")
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
        )

    audit_sender = audit.configure(resolved_key)

    agent_name = getattr(agent, "name", None) or "default"
    tools = _extract_tools(agent)
    tool_names = [tool.name for tool in tools]
    policy_set = build_policy_set(resolved_key, agent_name, tool_names)
    enforcer = PolicyEnforcer(
        policy_set, agent_name=agent_name, audit_sender=audit_sender
    )

    wrapped_tools = wrap_tools(tools, enforcer)
    cloned_agent = _clone_agent_with_tools(agent, wrapped_tools)

    return FortifyPydanticAgent(
        agent=cloned_agent,
        api_key=resolved_key,
        agent_name=agent_name,
    )
