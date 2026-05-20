"""Agent-level wrapping for the OpenAI Agents adapter.

Builds the :class:`~fortify.security.policy_set.PolicySet` for an agent
(today a stub — TODO to fetch from the Fortify control plane), constructs
one :class:`~fortify.security.enforcer.PolicyEnforcer` per agent, and
returns a clone of the agent whose tools are policy-gated via
:func:`fortify.adapters.openai.tools.wrap_tool`.

The wrapper itself is user-agnostic: role resolution happens inside the
enforcer at call time via the :class:`~fortify.runtime.User` contextvar,
so callers do not need to thread user identity through here.
"""

from __future__ import annotations

import dataclasses

from agents import Agent

from fortify.adapters.openai.tools import wrap_tools
from fortify.security import AgentPolicy, BaseToolPolicy, PolicySet
from fortify.security.enforcer import PolicyEnforcer
from fortify.security.policy_set import DEFAULT_ROLE_NAME


def build_policy_set(
    api_key: str,  # noqa: ARG001 — reserved for the future Fortify-cloud fetch
    agent_name: str,  # noqa: ARG001 — same
    tool_names: list[str],
) -> PolicySet:
    """Build the :class:`PolicySet` for an agent.

    Placeholder: returns a one-role bundle that allows every named tool.
    TODO: fetch the canonical PolicySet for ``agent_name`` from the
    Fortify control plane via :class:`~fortify.cloud.FortifyClient` and
    parse it with ``load_policy_set_from_dict`` — keeps the SDK and
    platform in sync on policy shape.
    """
    default_policy = AgentPolicy(
        tools={name: BaseToolPolicy(mode="allow") for name in tool_names}
    )
    return PolicySet({DEFAULT_ROLE_NAME: default_policy})


def wrap_openai_agent(agent: Agent, *, api_key: str) -> Agent:
    """Return a clone of ``agent`` whose tools are policy-gated.

    Role resolution and constraint evaluation happen lazily inside the
    enforcer at call time, reading the active
    :class:`~fortify.runtime.User` from the contextvar — so the caller
    must open a ``User`` scope (``async with User(...)`` or
    ``User(...).sync_scope()``) around the agent run.
    """
    agent_name = getattr(agent, "name", "default")
    tool_names = [tool.name for tool in agent.tools]
    policy_set = build_policy_set(api_key, agent_name, tool_names)
    enforcer = PolicyEnforcer(policy_set, agent_name=agent_name)
    guarded_tools = wrap_tools(agent.tools, enforcer)
    return dataclasses.replace(agent, tools=guarded_tools)
