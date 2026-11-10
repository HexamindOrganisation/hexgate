"""Similar agents: which registered agents look most like a given one, by
keyword, so a new policy can start from the closest agents' policies.

Four signals, each worth a fixed number of points (no embeddings): a tool both
manifests register by the same name, a word both agents' tool names contain, a
word both agents' tool descriptions contain, and a role both agents' policies
define (only in a project that has a project policy: without one every agent
has the same platform roles). A match reports each signal that fired, so the
caller sees why it ranked, and whether it has a policy to start from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.features.policy_modules import coverage
from hexgate_api.schemas import SimilarAgentOut, SimilarReasonsOut

if TYPE_CHECKING:  # the SDK is imported lazily at run time
    from hexgate.manifest.models import AgentManifest

POINTS_PER_TOOL = 3.0
POINTS_PER_NAME_WORD = 1.0
POINTS_PER_DESCRIPTION_WORD = 0.5
POINTS_PER_ROLE = 1.0

# Words too common in tool names and descriptions to say two agents are alike.
_MIN_WORD = 4
_STOPWORDS = frozenset(
    "this that with from when will your what then than into have been used "
    "using uses returns return given must only also each their them they "
    "which where while about after before other such some more most list".split()
)
_CAMEL = re.compile(r"([a-z0-9])([A-Z])|([A-Z])([A-Z][a-z])")


class UnknownAgentError(Exception):
    """The agent to compare against is not registered in the project (→ 404)."""


@dataclass(frozen=True)
class Profile:
    """What an agent is compared on."""

    tools: frozenset[str]
    name_words: frozenset[str]
    description_words: frozenset[str]
    roles: frozenset[str]

    @classmethod
    def of(cls, manifest: AgentManifest | None, roles: list[str]) -> Profile:
        """``roles`` are the policy's, ``default`` aside: every policy has it."""
        from hexgate.security import DEFAULT_ROLE_NAME

        tools = manifest.tools if manifest else []
        return cls(
            tools=frozenset(t.name for t in tools),
            name_words=frozenset(
                w for t in tools for w in _words(t.name) if _is_content(w)
            ),
            description_words=frozenset(
                w for t in tools for w in _words(t.description) if _is_content(w)
            ),
            roles=frozenset(roles) - {DEFAULT_ROLE_NAME},
        )


def _words(text: str) -> list[str]:
    """``text`` split on separators and camelCase, lowercased."""
    spaced = _CAMEL.sub(r"\1\3 \2\4", text)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def _is_content(word: str) -> bool:
    return len(word) >= _MIN_WORD and word not in _STOPWORDS and not word.isdigit()


def compare(target: Profile, other: Profile) -> tuple[float, SimilarReasonsOut]:
    """The points ``other`` earns against ``target``, and the signals behind
    them, each sorted so the answer is stable."""
    tools = sorted(target.tools & other.tools)
    # A shared tool name already shares its words: only the rest are new.
    covered = {w for name in tools for w in _words(name)}
    name_words = sorted((target.name_words & other.name_words) - covered)
    # A description repeats the tool's name: a word already counted as a name
    # word is not counted again.
    description_words = sorted(
        (target.description_words & other.description_words)
        - target.name_words
        - other.name_words
    )
    roles = sorted(target.roles & other.roles)
    score = (
        POINTS_PER_TOOL * len(tools)
        + POINTS_PER_NAME_WORD * len(name_words)
        + POINTS_PER_DESCRIPTION_WORD * len(description_words)
        + POINTS_PER_ROLE * len(roles)
    )
    return score, SimilarReasonsOut(
        shared_tools=tools,
        shared_name_words=name_words,
        shared_roles=roles,
        untrusted_shared_description_words=description_words,
    )


async def similar_agents(
    session: AsyncSession, project_id: str, agent: str, limit: int
) -> tuple[bool, list[SimilarAgentOut]]:
    """Whether ``agent`` has a registered manifest, and the other registered
    agents with any signal in common, best first (ties by name), at most
    ``limit``. Raises :class:`UnknownAgentError` for an agent that is not
    registered, and what :func:`coverage.agent_policies` raises when a policy
    does not resolve."""
    from hexgate_api.features.agents.service import latest_manifests

    modular, enforced = await coverage.agent_policies(session, project_id)
    if agent not in enforced:
        raise UnknownAgentError(agent)
    manifests = await latest_manifests(session, project_id)
    # Roles say something only where the project policy gave the agent a
    # section: without a project policy every agent's own one is seeded with
    # the same platform roles, and an agent it has no section for runs under
    # the "*" view, whose roles all such agents share.
    profiles = {
        name: Profile.of(
            manifests.get(name),
            policy.policy_set.roles if modular and policy.has_policy else [],
        )
        for name, policy in enforced.items()
    }
    target = profiles[agent]
    matches = []
    for name, profile in profiles.items():
        if name == agent:
            continue
        score, reasons = compare(target, profile)
        if score > 0:
            matches.append(
                SimilarAgentOut(
                    agent=name,
                    score=score,
                    has_policy=enforced[name].has_policy,
                    roles=sorted(profile.roles),
                    reasons=reasons,
                )
            )
    matches.sort(key=lambda m: (-m.score, m.agent))
    return agent in manifests, matches[:limit]
