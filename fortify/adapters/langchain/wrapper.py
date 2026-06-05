"""BYO-graph entry point: retrofit a pre-built ``CompiledStateGraph`` with
Fortify policy. Tools are mutated in place so the graph keeps its
references; the returned :class:`FortifyLangchainAgent` opens a User
scope + Langfuse propagation per call. For the manifest-driven path,
use :func:`fortify.enforce_policy` instead.

Policy is resolved from the platform at wrap time (register-on-404) and
refreshed by the proxy at the top of every call.
"""

from __future__ import annotations

import logging
import os

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from fortify import audit
from fortify.adapters.langchain.agent import FortifyLangchainAgent
from fortify.adapters.langchain.tools import install_enforcer_on_tools
from fortify.security.binding import PolicyBinding
from fortify.security.enforcer import PolicyEnforcer

logger = logging.getLogger("fortify.adapters.langchain")


def _resolve_binding(
    agent: CompiledStateGraph,
    tools: list[BaseTool],
    agent_name: str,
    api_key: str,
) -> PolicyBinding:
    """Resolve the platform policy for ``agent_name``, registering on 404.

    ``tools`` carries the real schemas (raw graphs don't expose their
    tool nodes). Non-404 failures stay loud — never a silent allow-all.
    """
    from fortify.cloud.client import FortifyError

    try:
        return PolicyBinding.resolve(agent_name, api_key=api_key)
    except FortifyError as exc:
        if exc.status != 404:
            raise
        from fortify.cli.register import register_agent

        logger.info("agent %r not registered — registering it from code", agent_name)
        register_agent(agent, tools=tools)
        return PolicyBinding.resolve(agent_name, api_key=api_key)


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

    binding = _resolve_binding(agent, tools, agent_name, resolved_key)
    # Rebuild the enforcer to inject the audit sender; the rebound binding
    # keeps the seeded source, so refresh swaps this enforcer in place.
    enforcer = PolicyEnforcer(
        binding.enforcer.policy,
        agent_name=agent_name,
        audit_sender=audit.configure(resolved_key),
    )
    binding = PolicyBinding(enforcer, binding.source)

    install_enforcer_on_tools(tools, enforcer=enforcer)

    return FortifyLangchainAgent(
        agent=agent,
        api_key=resolved_key,
        tool_names=tool_names,
        binding=binding,
    )
