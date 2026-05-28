"""BYO-graph entry point: retrofit a pre-built ``CompiledStateGraph`` with
Fortify policy. Tools are mutated in place so the graph keeps its
references; the returned :class:`FortifyLangchainAgent` opens a User
scope + Langfuse propagation per call. For the manifest-driven path,
use :func:`fortify.enforce_policy` instead.
"""

from __future__ import annotations

import os

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.adapters.langchain.tools import install_enforcer_on_tools
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


def wrap_langchain_agent(
    *,
    agent: CompiledStateGraph,
    tools: list[BaseTool],
    api_key: str | None = None,
) -> FortifyLangchainAgent:
    """Wrap a pre-built LangGraph agent with Fortify policy enforcement.

    Mutates ``tools`` in place so the graph keeps its references.
    The returned proxy takes ``user`` per invocation; role resolves at
    call time from the active :class:`User`. ``api_key`` falls back to
    ``FORTIFY_KEY``. ``NEEDS_APPROVAL`` outcomes render as structured
    errors — wire any host-side approval flow outside the SDK.
    """
    resolved_key = api_key if api_key else os.getenv("FORTIFY_KEY")
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
        )

    agent_name = getattr(agent, "name", "default")
    tool_names = [tool.name for tool in tools]
    policy_set = build_policy_set(resolved_key, agent_name, tool_names)
    enforcer = PolicyEnforcer(policy_set, agent_name=agent_name)

    install_enforcer_on_tools(tools, enforcer=enforcer)

    return FortifyLangchainAgent(
        agent=agent,
        api_key=resolved_key,
        tool_names=tool_names,
    )
