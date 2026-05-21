"""Entry point for retrofitting a pre-built LangGraph agent with Fortify.

:func:`wrap_langchain_agent` is for the BYO-agent path: the caller built
their own ``CompiledStateGraph`` via LangGraph and just wants Fortify
policy enforcement layered on. The tools are mutated in place via
:func:`~fortify.adapters.langchain.tools.install_enforcer_on_tool` so
the existing graph keeps its references; the returned
:class:`FortifyLangchainAgent` proxy binds a :class:`User` scope and
Langfuse propagation per call.

For the manifest-driven path (load an agent from disk or Fortify Cloud,
let the SDK build the graph), use :func:`fortify.enforce_policy` on the
returned :class:`FortifyAgent` instead.
"""

from __future__ import annotations

import os

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.adapters.langchain.tools import (
    ApprovalHandler,
    install_enforcer_on_tools,
)
from fortify.security import AgentPolicy, BaseToolPolicy, PolicySet
from fortify.security.enforcer import PolicyEnforcer
from fortify.security.policy_set import DEFAULT_ROLE_NAME


def build_policy_set(
    api_key: str,  # noqa: ARG001 — reserved for the future Fortify-cloud fetch
    agent_name: str,  # noqa: ARG001 — same
    tool_names: list[str],
) -> PolicySet:
    """Build the :class:`PolicySet` for a wrapped LangChain agent.

    Placeholder: returns a one-role bundle that allows every named tool.
    TODO: fetch the canonical ``policy_yaml`` for ``agent_name`` from the
    Fortify control plane via :class:`~fortify.cloud.FortifyClient` and
    parse it with ``load_policy_set_from_dict``.
    """
    default_policy = AgentPolicy(
        tools={name: BaseToolPolicy(mode="allow") for name in tool_names}
    )
    return PolicySet({DEFAULT_ROLE_NAME: default_policy})


def wrap_langchain_agent(
    *,
    agent: CompiledStateGraph,
    tools: list[BaseTool],
    api_key: str | None = None,
    approval_handler: ApprovalHandler | None = None,
) -> FortifyLangchainAgent:
    """Wrap a pre-built LangGraph agent with Fortify policy enforcement.

    Mutates the caller's tool instances in place — every ``func`` /
    ``coroutine`` is rebound to consult the new
    :class:`~fortify.security.enforcer.PolicyEnforcer` before delegating.
    The wrapped ``CompiledStateGraph`` keeps its existing tool
    references and acquires enforcement transparently.

    The returned proxy expects a ``user`` keyword argument on each
    invocation method (``invoke``, ``ainvoke``, ``stream``, ``astream``,
    ``astream_events``). Role resolution happens at call time from the
    active :class:`~fortify.runtime.User`.

    Args:
        agent: The compiled LangGraph agent to wrap.
        tools: The list of tools the agent was instantiated with. Mutated
            in place; the same list is read back by the proxy for the
            policy set's tool surface.
        api_key: The Fortify API key. Falls back to the ``FORTIFY_KEY``
            environment variable.
        approval_handler: Resolves ``NEEDS_APPROVAL`` outcomes inline.
            ``True`` / ``False`` short-circuit; a callable receives the
            :class:`Decision` and returns ``bool``. When ``None``,
            approval-required tool calls render as structured errors.
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

    install_enforcer_on_tools(
        tools, enforcer=enforcer, approval_handler=approval_handler
    )

    return FortifyLangchainAgent(
        agent=agent,
        api_key=resolved_key,
        tool_names=tool_names,
    )
