"""BYO-graph entry point: retrofit a pre-built ``CompiledStateGraph`` with
Fortify policy. Tools are mutated in place so the graph keeps its
references; the returned :class:`FortifyLangchainAgent` opens a User
scope + Langfuse propagation per call. For the manifest-driven path,
use :func:`fortify.enforce_policy` instead.

Policy is resolved from the platform at wrap time (register-on-404) and
refreshed by the proxy at the top of every call.
"""

from __future__ import annotations

import os

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from fortify import audit
from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.adapters.langchain.tools import install_enforcer_on_tools
from fortify.security.binding import PolicyBinding, resolve_policy_or_register
from fortify.security.enforcer import PolicyEnforcer


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
    errors — wire any host-side approval flow outside the SDK. The
    enforced policy is the platform's; unlisted tools are denied.
    """
    resolved_key = api_key if api_key else os.getenv("FORTIFY_KEY")
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
        )

    agent_name = getattr(agent, "name", "default")
    tool_names = [tool.name for tool in tools]

    # tools carries the real schemas — raw graphs don't expose their nodes.
    def _register() -> None:
        from fortify.cli.register import register_agent

        register_agent(agent, tools=tools)

    resolved = resolve_policy_or_register(
        agent_name, api_key=resolved_key, on_missing=_register
    )
    enforcer = PolicyEnforcer(
        resolved.engine,
        agent_name=agent_name,
        audit_sender=audit.configure(resolved_key),
    )
    install_enforcer_on_tools(tools, enforcer=enforcer)

    return FortifyLangchainAgent(
        agent=agent,
        api_key=resolved_key,
        tool_names=tool_names,
        binding=PolicyBinding(enforcer, resolved.source),
    )
