"""Policy coverage: which tools and sub-agents in each agent's manifest no rule
names. A call to such a tool falls to the policy's default; reach to such a
sub-agent is closed-world, so it is denied where the policy gates reach and not
checked at all where it doesn't.

A tool counts as covered when any role's resolved policy names it, including
a role no user holds today: the question is whether a rule exists, not who
reaches it. This reports facts, not problems; the lints stay with
``analyze_policy`` (R-POL-003).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.features.policy_modules import service
from hexgate_api.models import Agent
from hexgate_api.schemas import (
    AgentCoverageOut,
    UncoveredSubagentOut,
    UncoveredToolOut,
)

if TYPE_CHECKING:  # the SDK is imported lazily at run time
    from hexgate.manifest.models import AgentManifest
    from hexgate.security.policy_set import PolicySet


class NotModularError(Exception):
    """The project has no entry file and no role binding, so each agent
    enforces its own ``policy_yaml`` and there is no project policy to cover."""


async def project_coverage(
    session: AsyncSession, project_id: str
) -> list[AgentCoverageOut]:
    """Coverage for every registered agent, by name. Raises
    :class:`NotModularError`, or the SDK's compose errors
    (:func:`service.compose_error_types`) when the project doesn't resolve."""
    from hexgate_api.features.agents.service import latest_manifests

    if not await service.is_modular(session, project_id):
        raise NotModularError
    resolved = await service.resolve_every_agent(session, project_id)
    # Every agent without a section shares the "*" resolution: name its keys once.
    named = {agent: _named_keys(r.policy_set) for agent, r in resolved.items()}
    names = (
        await session.exec(
            select(Agent.name)
            .where(Agent.project_id == project_id)
            .order_by(Agent.name)  # type: ignore[arg-type]
        )
    ).all()
    manifests = await latest_manifests(session, project_id)
    return [
        _agent_coverage(name, resolved, named, manifests.get(name)) for name in names
    ]


def _agent_coverage(
    agent: str,
    resolved: dict,
    named: dict[str, set[str]],
    manifest: AgentManifest | None,
) -> AgentCoverageOut:
    has_policy = agent in resolved
    column = agent if has_policy else service.DEFAULT_AGENT
    policy_set: PolicySet = resolved[column].policy_set
    tools: list[UncoveredToolOut] = []
    subagents: list[UncoveredSubagentOut] = []
    if manifest is not None:
        tools = _uncovered_tools(manifest, named[column])
        subagents = _uncovered_subagents(
            manifest, named[column], tool_reach=policy_set.declares_tool_reach()
        )
    return AgentCoverageOut(
        agent=agent,
        has_policy=has_policy,
        has_manifest=manifest is not None,
        default_policy=policy_set.policy_for(None).default_policy.mode,
        reach_gated=policy_set.declares_reach(),
        uncovered_tools=tools,
        uncovered_subagents=subagents,
    )


def _named_keys(policy_set: PolicySet) -> set[str]:
    """Every tool key some role's resolved policy has a rule for."""
    return {
        key
        for role in policy_set.roles
        for key in policy_set.policy_for(role).effective_tools
    }


def _uncovered_tools(
    manifest: AgentManifest, named: set[str]
) -> list[UncoveredToolOut]:
    """The manifest's tools no rule names, in manifest order (the platform
    stores a tool name once per manifest)."""
    return [
        UncoveredToolOut(tool=t.name, untrusted_description=t.description)
        for t in manifest.tools
        if t.name not in named
    ]


def _uncovered_subagents(
    manifest: AgentManifest, named: set[str], *, tool_reach: bool
) -> list[UncoveredSubagentOut]:
    """The manifest's sub-agents with a reach edge no rule names, once each
    even when both of its edges (``tool`` and ``handoff``) are uncovered.

    A ``tool`` edge counts only once the policy declares tool reach: until
    then the adapters decide that call under its tool name, which
    :func:`_uncovered_tools` already judges, and its reach key is never read."""
    from hexgate.security.models import agent_target_key
    from hexgate.security.naming import canonical_name

    edges = ((canonical_name(ref.name), ref.via) for ref in manifest.subagents or ())
    uncovered = dict.fromkeys(
        name
        for name, via in edges
        if (via != "tool" or tool_reach) and agent_target_key(via, name) not in named
    )
    return [UncoveredSubagentOut(agent=name) for name in uncovered]
