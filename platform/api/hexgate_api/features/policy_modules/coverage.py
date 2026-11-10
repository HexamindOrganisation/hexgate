"""Policy coverage: which tools and sub-agents in each agent's manifest no rule
names. A call to such a tool falls to its role's default (the report gives
the most permissive one any role applies); reach to such a
sub-agent is closed-world, so it is denied where the policy gates reach and not
checked at all where it doesn't.

A tool counts as covered when any role's resolved policy names it, including
a role no user holds today: the question is whether a rule exists, not who
reaches it. This reports facts, not problems; the lints stay with
``analyze_policy`` (R-POL-003).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml
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


class PolicyLoadError(Exception):
    """An agent's stored ``policy_yaml`` does not load (a classic project)."""

    def __init__(self, agent: str) -> None:
        super().__init__(agent)
        self.agent = agent


@dataclass(frozen=True)
class EnforcedPolicy:
    """The policy set an agent enforces, the tool keys it has a rule for, and
    whether it is the agent's own (its section of the project policy, or its
    own stored ``policy_yaml``)."""

    policy_set: PolicySet
    named: set[str]
    has_policy: bool

    @classmethod
    def of(cls, policy_set: PolicySet, *, has_policy: bool) -> EnforcedPolicy:
        return cls(policy_set, _named_keys(policy_set), has_policy)


async def agent_policies(
    session: AsyncSession, project_id: str
) -> tuple[bool, dict[str, EnforcedPolicy]]:
    """Whether the project has a policy of its own (``is_modular``), and every
    registered agent's :class:`EnforcedPolicy`, by name: its section of that
    policy, else its own ``policy_yaml``. Raises the SDK's compose errors
    (:func:`service.compose_error_types`) when the project policy doesn't
    resolve, and :class:`PolicyLoadError` when an agent's own doesn't load."""
    agents = (
        await session.exec(
            select(Agent.name, Agent.policy_yaml)
            .where(Agent.project_id == project_id)
            .order_by(Agent.name)  # type: ignore[arg-type]
        )
    ).all()
    if await service.is_modular(session, project_id):
        enforced = await _project_policies(session, project_id, [a for a, _ in agents])
        return True, enforced
    return False, {agent: _own_policy(agent, text) for agent, text in agents}


async def project_coverage(
    session: AsyncSession, project_id: str
) -> list[AgentCoverageOut]:
    """Coverage for every registered agent, by name, against the policy it
    enforces (:func:`agent_policies`, whose errors it raises)."""
    from hexgate_api.features.agents.service import latest_manifests

    _, enforced = await agent_policies(session, project_id)
    manifests = await latest_manifests(session, project_id)
    return [
        _agent_coverage(agent, policy, manifests.get(agent))
        for agent, policy in enforced.items()
    ]


async def _project_policies(
    session: AsyncSession, project_id: str, agents: list[str]
) -> dict[str, EnforcedPolicy]:
    """Each agent's section of the project policy, or the ``"*"`` view when
    the policy has none for it."""
    resolved = await service.resolve_every_agent(session, project_id)
    generic = EnforcedPolicy.of(
        resolved[service.DEFAULT_AGENT].policy_set, has_policy=False
    )
    return {
        agent: EnforcedPolicy.of(resolved[agent].policy_set, has_policy=True)
        if agent in resolved
        else generic
        for agent in agents
    }


def _own_policy(agent: str, policy_yaml: str) -> EnforcedPolicy:
    """An agent's own stored policy. An empty one loads as the platform
    default (deny everything), which is what the agent then enforces."""
    from hexgate.security import load_policy_set_from_dict

    try:
        document = yaml.safe_load(policy_yaml) or {}
        if not isinstance(document, dict):  # a row saved before saves validated
            raise PolicyLoadError(agent)
        policy_set = load_policy_set_from_dict(document)
    except service.compose_error_types() as exc:
        raise PolicyLoadError(agent) from exc
    return EnforcedPolicy.of(policy_set, has_policy=bool(policy_yaml.strip()))


def _agent_coverage(
    agent: str,
    enforced: EnforcedPolicy,
    manifest: AgentManifest | None,
) -> AgentCoverageOut:
    policy_set, keys = enforced.policy_set, enforced.named
    tools: list[UncoveredToolOut] = []
    subagents: list[UncoveredSubagentOut] = []
    if manifest is not None:
        tools = _uncovered_tools(manifest, keys)
        subagents = _uncovered_subagents(
            manifest, keys, tool_reach=policy_set.declares_tool_reach()
        )
    return AgentCoverageOut(
        agent=agent,
        has_policy=enforced.has_policy,
        has_manifest=manifest is not None,
        default_policy=_most_permissive_default(policy_set),
        reach_gated=policy_set.declares_reach(),
        uncovered_tools=tools,
        uncovered_subagents=subagents,
    )


def _most_permissive_default(policy_set: PolicySet) -> str:
    """The most permissive default any role applies to an uncovered tool: the
    worst case a gap can reach. Roles of an agent's own policy may differ. A
    mode the SDK's ranking lacks counts as the loosest, never as a deny."""
    from hexgate.security.analyzer import MODE_STRICTNESS

    modes = {
        policy_set.policy_for(role).default_policy.mode for role in policy_set.roles
    }
    return min(modes, key=lambda mode: MODE_STRICTNESS.get(mode, -1))


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
