"""Google ADK adapter: build a :class:`PolicySet`, construct one
:class:`PolicyEnforcer`, and return a clone of the agent whose tools
are policy-gated. User-agnostic at wrap time — role resolution happens
inside the enforcer via the :class:`User` contextvar.
"""

from __future__ import annotations

from google.adk.agents import BaseAgent

from fortify import audit
from fortify.adapters.google.tools import wrap_tools
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


def wrap_google_agent(agent: BaseAgent, *, api_key: str) -> BaseAgent:
    """Return a clone of ``agent`` with policy-gated tools.

    Caller must open a :class:`User` scope around the run.
    ``NEEDS_APPROVAL`` outcomes surface as ``[approval_required]``-prefixed
    strings in tool results; ``[policy_denied]`` for denials.
    """
    audit.configure(api_key)

    agent_name = getattr(agent, "name", "default")
    tools = list(getattr(agent, "tools", []) or [])
    tool_names = [getattr(t, "name", getattr(t, "__name__", "tool")) for t in tools]
    policy_set = build_policy_set(api_key, agent_name, tool_names)
    enforcer = PolicyEnforcer(policy_set, agent_name=agent_name)
    guarded_tools = wrap_tools(tools, enforcer)
    return agent.model_copy(update={"tools": guarded_tools})
