"""BYO-graph entry point: retrofit a pre-built ``CompiledStateGraph`` with
Fortify policy. Tools are mutated in place so the graph keeps its
references; the returned :class:`FortifyLangchainAgent` opens a User
scope + Langfuse propagation per call. For the manifest-driven path,
use :func:`fortify.enforce_policy` instead.

Policy comes from the platform (policy-binding spec, phase 4): wrap-time
:meth:`~fortify.security.binding.PolicyBinding.resolve` pulls + verifies
the current policy (``FORTIFY_LOCAL_POLICY`` override → signed bundle →
pydantic fallback), an unknown agent is registered from its in-code
definition and resolved again, and the proxy refreshes the binding at
the top of every call — so dashboard edits land at the next run with a
cheap ETag/304 round trip.
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

    An unknown agent exists only in code so far: register it from its own
    definition (``tools`` carries the real schemas — raw LangGraph graphs
    don't expose their tool nodes reliably) and resolve again; the
    platform answers a first register with a default role-aware policy +
    signed bundle. Anything else (bad key, bad signature, platform down)
    stays loud — wrapping asked for governance, so failing to bind is an
    error, never a silently allow-all agent.
    """
    from fortify.cloud.client import FortifyError

    try:
        return PolicyBinding.resolve(agent_name, api_key=api_key)
    except FortifyError as exc:
        if exc.status != 404:
            raise
        from fortify.cli.register import register_agent

        logger.info(
            "agent %r not registered on the platform — registering it from "
            "the in-code graph definition",
            agent_name,
        )
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
    errors — wire any host-side approval flow outside the SDK.

    The enforced policy is the platform's (see :func:`_resolve_binding`),
    and the proxy re-pulls it at the top of every call. Tools not named
    in that policy are denied-by-absence at call time.
    """
    resolved_key = api_key if api_key else os.getenv("FORTIFY_KEY")
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
        )

    agent_name = getattr(agent, "name", "default")
    tool_names = [tool.name for tool in tools]

    binding = _resolve_binding(agent, tools, agent_name, resolved_key)
    # Rebuild the enforcer around the resolved engine so the adapter's
    # audit sender rides along — resolve() is adapter-agnostic and doesn't
    # know about audit. The rebound binding keeps the seeded source, so
    # refresh still swaps THIS enforcer's policy in place.
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
