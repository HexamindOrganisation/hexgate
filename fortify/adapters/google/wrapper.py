"""Agent-level wrapping for the Google ADK adapter.

Builds the :class:`~fortify.security.policy_set.PolicySet` for an agent
(today a stub — TODO to fetch from the Fortify control plane), constructs
one :class:`~fortify.security.enforcer.PolicyEnforcer` per agent, and
returns a clone of the agent whose tools are policy-gated via
:func:`fortify.adapters.google.tools.wrap_tool`.

The wrapper itself is user-agnostic: role resolution happens inside the
enforcer at call time via the :class:`~fortify.runtime.User` contextvar,
so callers do not need to thread user identity through here.
"""

from __future__ import annotations

from google.adk.agents import BaseAgent

from fortify.adapters.google.tools import wrap_tools
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
    TODO: fetch the canonical ``policy_yaml`` for ``agent_name`` from the
    Fortify control plane via :class:`~fortify.cloud.FortifyClient`.
    """
    default_policy = AgentPolicy(
        tools={name: BaseToolPolicy(mode="allow") for name in tool_names}
    )
    return PolicySet({DEFAULT_ROLE_NAME: default_policy})


def wrap_google_agent(agent: BaseAgent, *, api_key: str) -> BaseAgent:
    """Return a clone of ``agent`` whose tools are policy-gated.

    Role resolution and constraint evaluation happen lazily inside the
    enforcer at call time, reading the active
    :class:`~fortify.runtime.User` from the contextvar — so the caller
    must open a ``User`` scope (``async with User(...)`` or
    ``User(...).sync_scope()``) around the agent run.

    ``NEEDS_APPROVAL`` outcomes render as structured strings via
    :func:`~fortify.adapters.google.tools._render_decision`; the host
    can recognize the ``[approval_required]`` marker in tool results.
    """
    agent_name = getattr(agent, "name", "default")
    tools = list(getattr(agent, "tools", []) or [])
    tool_names = [getattr(t, "name", getattr(t, "__name__", "tool")) for t in tools]
    policy_set = build_policy_set(api_key, agent_name, tool_names)
    enforcer = PolicyEnforcer(policy_set, agent_name=agent_name)
    guarded_tools = wrap_tools(tools, enforcer)
    return agent.model_copy(update={"tools": guarded_tools})
