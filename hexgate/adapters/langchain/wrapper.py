"""BYO-graph entry point: retrofit a pre-built ``CompiledStateGraph`` with
Hexgate policy. Tools are mutated in place so the graph keeps its
references; the returned :class:`HexgateLangchainAgent` opens a HexgateContext
scope + Langfuse propagation per call. For the manifest-driven path,
use :func:`hexgate.enforce_policy` instead.

Policy is resolved from the platform at wrap time (fail-loud on a 404 —
register the agent first with ``hexgate register``) and refreshed by the
proxy at the top of every call.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from hexgate.adapters.langchain.agent import HexgateLangchainAgent
from hexgate.adapters.langchain.tools import (
    install_enforcer_on_tool,
    install_enforcer_on_tools,
)
from hexgate.cloud.client import HexgateClient, HexgateConfig
from hexgate.config.env import resolve_api_key
from hexgate.guards.types import ToolPipeline, build_pipeline
from hexgate.manifest.langchain import discover_graph_tools
from hexgate.security.agent_gate import (
    warn_if_admission_unenforced,
    warn_if_reach_unenforced,
)
from hexgate.security.bans import resolve_ban_gate
from hexgate.security.binding import PolicyBinding, resolve_policy
from hexgate.security.enforcer import PolicyEnforcer, build_enforcer
from hexgate.security.naming import canonical_agent_name

if TYPE_CHECKING:
    from hexgate.guards.types import Guard, GuardObserver

_log = logging.getLogger(__name__)


def wrap_langchain_agent(
    *,
    agent: CompiledStateGraph,
    tools: list[BaseTool],
    api_key: str | None = None,
    guards: Sequence[Guard] | None = None,
    guard_observer: GuardObserver | None = None,
) -> HexgateLangchainAgent:
    """Wrap a pre-built LangGraph agent with Hexgate policy enforcement.

    Mutates ``tools`` in place so the graph keeps its references. Tools bound
    in the graph but absent from ``tools`` (e.g. middleware-injected) are gated
    too; one that cannot be gated is skipped with a warning rather than raising.
    The returned proxy takes ``hexgate_context`` per invocation; role resolves at
    call time from the active :class:`HexgateContext`. ``api_key`` falls back to
    ``HEXGATE_API_KEY``. ``NEEDS_APPROVAL`` outcomes render as structured
    errors — wire any host-side approval flow outside the SDK. ``guards`` is a flat
    list of guards authored with ``@before_tool`` / ``@after_tool``; they wrap each
    tool in place alongside the policy (``guard_observer`` receives their provenance
    events). The enforced policy is the platform's; unlisted tools are denied.
    """
    resolved_key = resolve_api_key(api_key)
    if not resolved_key:
        raise ValueError(
            "No API key provided. Pass api_key= explicitly or set the HEXGATE_API_KEY environment variable."
        )

    agent_name = canonical_agent_name(agent)
    caller_names = {tool.name for tool in tools}
    discovered = [
        tool for tool in discover_graph_tools(agent) if tool.name not in caller_names
    ]
    tool_names = [tool.name for tool in [*tools, *discovered]]

    # One client shared by the policy and ban resolvers — avoids a second
    # biscuit verify + JWKS round-trip per wrapped agent.
    client = HexgateClient(HexgateConfig.from_env(api_key=resolved_key))
    resolved = resolve_policy(agent_name, api_key=resolved_key, client=client)
    enforcer = build_enforcer(
        resolved.engine, agent_name=agent_name, api_key=resolved_key
    )
    warn_if_admission_unenforced(
        resolved.engine, framework="LangChain", agent_name=agent_name
    )
    warn_if_reach_unenforced(
        resolved.engine, framework="LangChain", agent_name=agent_name
    )
    pipeline = build_pipeline(guards, observer=guard_observer)
    install_enforcer_on_tools(tools, enforcer=enforcer, pipeline=pipeline)
    _install_on_discovered(discovered, enforcer=enforcer, pipeline=pipeline)

    return HexgateLangchainAgent(
        agent=agent,
        api_key=resolved_key,
        agent_name=agent_name,
        tool_names=tool_names,
        binding=PolicyBinding(enforcer, resolved.source),
        ban_gate=resolve_ban_gate(agent_name, api_key=resolved_key, client=client),
    )


def _install_on_discovered(
    tools: list[BaseTool],
    *,
    enforcer: PolicyEnforcer,
    pipeline: ToolPipeline | None,
) -> None:
    """Gate graph-discovered tools, skipping any that cannot be gated.

    Lenient where install_enforcer_on_tools is strict: the caller did not choose
    these, so one exotic tool must not stop the rest of the graph being gated.
    """
    for tool in tools:
        try:
            install_enforcer_on_tool(tool, enforcer=enforcer, pipeline=pipeline)
        except TypeError:
            _log.warning(
                "tool %r is bound to the graph but cannot be gated; it will run "
                "ungoverned",
                tool.name,
            )
