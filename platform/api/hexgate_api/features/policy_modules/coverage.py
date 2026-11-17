"""Policy coverage: which tools in each agent's manifest a role leaves to a
default looser than deny, or no rule names, and which sub-agents no rule
names. A call to such a tool falls to its role's default (the report gives
the most permissive one any role applies); reach to such a
sub-agent is closed-world, so it is denied where the policy gates reach and not
checked at all where it doesn't.

A tool counts as covered when some role's resolved policy names it, including
a role no user holds today, and no role whose default is looser than deny
leaves it to that default: the question is whether a call to it can get past
a rule nobody wrote, not who reaches it. This reports facts, not problems; the lints stay with
``analyze_policy`` (R-POL-003).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
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


@dataclass(frozen=True)
class _Enforced:
    """The policy set an agent enforces, the keys some role has a rule for,
    the tool keys it covers (:func:`_covered_keys`), and whether it is the
    agent's own (its section of the project policy, or its own stored
    ``policy_yaml``)."""

    policy_set: PolicySet
    named: set[str]
    covered: set[str]
    has_policy: bool

    @classmethod
    def of(cls, policy_set: PolicySet, *, has_policy: bool) -> _Enforced:
        named, covered = _covered_keys(policy_set)
        return cls(policy_set, named, covered, has_policy)


async def project_coverage(
    session: AsyncSession, project_id: str, agent: str | None = None
) -> list[AgentCoverageOut]:
    """Coverage for every registered agent (or only ``agent``), by name,
    against the policy it enforces: the project policy when the project has
    one (``is_modular``), else its own ``policy_yaml``. Raises the SDK's
    compose errors (:func:`service.compose_error_types`) when the project
    policy doesn't resolve; an agent whose own policy doesn't load gets an
    entry marked ``policy_error`` instead, so it doesn't hide the others."""
    from hexgate_api.features.agents.service import latest_manifests

    enforced: dict[str, _Enforced | None]
    if await service.is_modular(session, project_id):
        names = (await session.exec(_agents(project_id, agent, Agent.name))).all()
        enforced = await _project_policies(session, project_id, list(names))
    else:
        rows = await session.exec(
            _agents(project_id, agent, Agent.name, Agent.policy_yaml)
        )
        enforced = _own_policies(rows.all())
        names = list(enforced)
    manifests = await latest_manifests(session, project_id, list(names))
    return [
        _agent_coverage(name, enforced[name], manifests.get(name)) for name in names
    ]


def _agents(project_id: str, agent: str | None, *columns):
    """``columns`` of the project's agents (or the one named ``agent``), by name."""
    stmt = select(*columns).where(Agent.project_id == project_id)
    if agent is not None:
        stmt = stmt.where(Agent.name == agent)
    return stmt.order_by(Agent.name)  # type: ignore[arg-type]


async def _project_policies(
    session: AsyncSession, project_id: str, agents: list[str]
) -> dict[str, _Enforced | None]:
    """Each agent's section of the project policy, or the ``"*"`` view when
    the policy declares none for it (as :func:`service.risk_facts` reads it),
    each resolved once."""
    files = await service._files_map(session, project_id)
    declared, resolve_for_agent, _ = await service._agent_resolver(
        session, project_id, files
    )
    resolve = cache(resolve_for_agent)
    return {
        agent: _Enforced.of(
            resolve(agent if agent in declared else service.DEFAULT_AGENT).policy_set,
            has_policy=agent in declared,
        )
        for agent in agents
    }


def _own_policies(rows) -> dict[str, _Enforced | None]:
    """Each agent's own stored policy, by name in ``rows``' order, loaded once
    per distinct text: agents seeded alike share one."""
    loaded = {text: _own_policy(text) for text in {text for _, text in rows}}
    return {name: loaded[text] for name, text in rows}


def _own_policy(policy_yaml: str) -> _Enforced | None:
    """An agent's own stored policy, or None when it does not load (a row
    saved before saves validated). An empty one loads as the platform default
    (deny everything), which is what the agent then enforces."""
    from hexgate.security import load_policy_set_from_dict

    try:
        document = yaml.safe_load(policy_yaml) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(document, dict) or not _role_keys_are_names(document):
        return None
    try:
        policy_set = load_policy_set_from_dict(document)
    except service.compose_error_types():
        return None
    return _Enforced.of(policy_set, has_policy=bool(policy_yaml.strip()))


def _role_keys_are_names(document: dict) -> bool:
    """Whether every role key is a string: YAML reads ``on:`` or ``1:`` as a
    bool or an int, which the SDK cannot sort beside the other roles."""
    roles = document.get("roles")
    return not isinstance(roles, dict) or all(isinstance(r, str) for r in roles)


def _agent_coverage(
    agent: str,
    enforced: _Enforced | None,
    manifest: AgentManifest | None,
) -> AgentCoverageOut:
    if enforced is None:
        return AgentCoverageOut(
            agent=agent,
            has_policy=True,
            has_manifest=manifest is not None,
            policy_error=True,
        )
    policy_set = enforced.policy_set
    tools: list[UncoveredToolOut] = []
    subagents: list[UncoveredSubagentOut] = []
    if manifest is not None:
        tools = _uncovered_tools(manifest, enforced.covered)
        subagents = _uncovered_subagents(
            manifest, enforced.named, tool_reach=policy_set.declares_tool_reach()
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
    modes = {
        policy_set.policy_for(role).default_policy.mode for role in policy_set.roles
    }
    return min(modes, key=_strictness)


def _strictness(mode: str) -> int:
    """The SDK's rank of ``mode``, deny strictest; a mode it lacks ranks loosest."""
    from hexgate.security.analyzer import MODE_STRICTNESS

    return MODE_STRICTNESS.get(mode, -1)


def _covered_keys(policy_set: PolicySet) -> tuple[set[str], set[str]]:
    """Every key some role's resolved policy has a rule for, and the covered
    ones: also named by every role whose default is looser than deny. A role's
    own default decides a tool it has no rule for, so a rule in another role
    does not close that role's gap."""
    policies = [policy_set.policy_for(role) for role in policy_set.roles]
    named = {key for policy in policies for key in policy.effective_tools}
    covered = set(named)
    for policy in policies:
        if _strictness(policy.default_policy.mode) < _strictness("deny"):
            covered &= set(policy.effective_tools)
    return named, covered


def _uncovered_tools(
    manifest: AgentManifest, covered: set[str]
) -> list[UncoveredToolOut]:
    """The manifest's tools the policy does not cover (:func:`_covered_keys`),
    in manifest order (the platform stores a tool name once per manifest)."""
    return [
        UncoveredToolOut(tool=t.name, untrusted_description=t.description)
        for t in manifest.tools
        if t.name not in covered
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
