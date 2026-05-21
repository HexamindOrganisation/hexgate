"""OpenAI Agents adapter: build a :class:`PolicySet`, construct one
:class:`PolicyEnforcer`, and return a clone of the agent whose tools
are policy-gated. User-agnostic at wrap time — role resolution happens
inside the enforcer via the :class:`User` contextvar.
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
    """Placeholder allow-all one-role bundle. TODO: cloud-fetch via FortifyClient."""
    default_policy = AgentPolicy(
        tools={name: BaseToolPolicy(mode="allow") for name in tool_names}
    )
    return PolicySet({DEFAULT_ROLE_NAME: default_policy})


def wrap_openai_agent(agent: Agent, *, api_key: str) -> Agent:
    """Return a clone of ``agent`` with policy-gated tools.

    Caller must open a :class:`User` scope around the run — role/constraints
    resolve at call time from the contextvar.
    """
    agent_name = getattr(agent, "name", "default")
    tool_names = [tool.name for tool in agent.tools]
    policy_set = build_policy_set(api_key, agent_name, tool_names)
    enforcer = PolicyEnforcer(policy_set, agent_name=agent_name)
    guarded_tools = wrap_tools(agent.tools, enforcer)
    return dataclasses.replace(agent, tools=guarded_tools)
