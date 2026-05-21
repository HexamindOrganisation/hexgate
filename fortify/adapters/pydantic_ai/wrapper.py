"""Entry point for wrapping a pydantic_ai ``Agent`` with Fortify policy.

Builds the :class:`~fortify.security.policy_set.PolicySet` for the
agent (today a stub — TODO to fetch from the Fortify control plane),
constructs one :class:`~fortify.security.enforcer.PolicyEnforcer`, and
returns a :class:`FortifyPydanticAgent` proxy whose underlying ``Agent``
is a clone of the caller's with policy-gated tools.
"""

from __future__ import annotations

import copy
import os

from pydantic_ai import Agent
from pydantic_ai.tools import Tool

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
    """Build the :class:`PolicySet` for a pydantic_ai agent.

    Placeholder: returns a one-role bundle that allows every named tool.
    TODO: fetch the canonical ``policy_yaml`` for ``agent_name`` from the
    Fortify control plane via :class:`~fortify.cloud.FortifyClient`.
    """
    default_policy = AgentPolicy(
        tools={name: BaseToolPolicy(mode="allow") for name in tool_names}
    )
    return PolicySet({DEFAULT_ROLE_NAME: default_policy})


def _extract_tools(agent: Agent) -> list[Tool]:
    """Extract the Tool instances registered on ``agent``.

    Pydantic AI normalizes both constructor-passed tools and
    ``@agent.tool`` / ``@agent.tool_plain`` registrations into the
    same ``_function_toolset.tools`` dict, keyed by tool name.
    """
    toolset = getattr(agent, "_function_toolset", None)
    tools = getattr(toolset, "tools", None) if toolset is not None else None
    if tools is None:
        return []
    return list(tools.values())


def _clone_agent_with_tools(agent: Agent, wrapped_tools: list[Tool]) -> Agent:
    """Return a shallow copy of ``agent`` whose function toolset holds ``wrapped_tools``."""
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
    """Wrap a pydantic_ai agent with Fortify tool policy and observability.

    Returns a :class:`FortifyPydanticAgent` backed by a clone of
    ``agent`` whose tools are gated by a freshly built
    :class:`PolicyEnforcer`. The caller's original ``agent`` is not
    mutated, so it can be reused or wrapped again independently.

    The returned proxy expects a ``user`` keyword argument on each
    invocation method (``run``, ``run_sync``, ``run_stream``, ``iter``).
    Role resolution happens at call time from the active
    :class:`~fortify.runtime.User`. ``NEEDS_APPROVAL`` outcomes raise
    :class:`~pydantic_ai.exceptions.ModelRetry` with an
    ``[approval_required]`` marker so the LLM sees the failure as a
    tool-result message.

    Args:
        agent: The pydantic_ai agent to wrap. Tools are read directly off
            the agent, so any tool registered via the constructor or via
            ``@agent.tool`` / ``@agent.tool_plain`` is gated.
        api_key: The Fortify API key. Falls back to the ``FORTIFY_KEY``
            environment variable.
    """
    resolved_key = api_key or os.getenv("FORTIFY_KEY")
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
        )

    agent_name = getattr(agent, "name", None) or "default"
    tools = _extract_tools(agent)
    tool_names = [tool.name for tool in tools]
    policy_set = build_policy_set(resolved_key, agent_name, tool_names)
    enforcer = PolicyEnforcer(policy_set, agent_name=agent_name)

    wrapped_tools = wrap_tools(tools, enforcer)
    cloned_agent = _clone_agent_with_tools(agent, wrapped_tools)

    return FortifyPydanticAgent(
        agent=cloned_agent,
        api_key=resolved_key,
        agent_name=agent_name,
        tool_names=tool_names,
    )
