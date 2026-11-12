"""Similar agents: which registered agents look most like a given one, by their
tool names and roles, so a new policy can start from the closest agents'
policies. Tool descriptions are not compared.

No embeddings. Each pair of agents gets three similarities between 0 and 1,
one per signal: the tool names both register, the words of those names, and
the roles both agents' policies define (only in a project that has a project
policy: without one every agent has the same platform roles). A similarity is
a weighted Jaccard, shared weight over union weight, so an agent with many
tools does not outscore a small one just by having more chances to overlap. A
feature weighs more the fewer agents in the project have it, so a word every
agent has adds almost nothing. The score is the weighted sum of the three. A
match reports each signal and what was shared, so the caller sees why it
ranked, and whether it has a policy to start from.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.features.policy_modules import coverage
from hexgate_api.schemas import SimilarAgentOut, SimilarReasonsOut, SimilarSignalsOut

if TYPE_CHECKING:  # the SDK is imported lazily at run time
    from hexgate.manifest.models import AgentManifest

WEIGHT_TOOLS = 0.7
WEIGHT_NAME_WORDS = 0.2
WEIGHT_ROLES = 0.1
SIGNAL_WEIGHTS = {
    "tools": WEIGHT_TOOLS,
    "name_words": WEIGHT_NAME_WORDS,
    "roles": WEIGHT_ROLES,
}
NO_ROLE_WEIGHTS = {k: w for k, w in SIGNAL_WEIGHTS.items() if k != "roles"}

_MIN_WORD = 4
_MIN_PLURAL_WORD = 5
_CAMEL = re.compile(r"([a-z0-9])([A-Z])|([A-Z])([A-Z][a-z])")
# "es" is the plural only after x, z, ch, sh or ss (boxes, branches, addresses);
# elsewhere the plural is the "s" (invoices, databases).
_ES_PLURAL = re.compile(r"(?:x|z|ch|sh|ss)es$")


class UnknownAgentError(Exception):
    """The agent to compare against is not registered in the project (→ 404)."""


@dataclass(frozen=True)
class Profile:
    """What an agent is compared on."""

    tools: frozenset[str]
    name_words: frozenset[str]
    roles: frozenset[str]

    @classmethod
    def of(cls, manifest: AgentManifest | None, roles: list[str]) -> Profile:
        """``roles`` are the policy's, ``default`` aside: every policy has it."""
        from hexgate.security import DEFAULT_ROLE_NAME

        names = [t.name for t in manifest.tools] if manifest else []
        return cls(
            tools=frozenset(names),
            name_words=frozenset(
                w for name in names for w in _words(name) if _is_content(w)
            ),
            roles=frozenset(roles) - {DEFAULT_ROLE_NAME},
        )


def _words(text: str) -> list[str]:
    """``text`` split on separators and camelCase, lowercased, plurals stripped."""
    spaced = _CAMEL.sub(r"\1\3 \2\4", text)
    split = re.split(r"[^A-Za-z0-9]+", spaced)
    return [_singular(w.lower()) for w in split if w]


def _singular(word: str) -> str:
    """``invoices`` → ``invoice``, ``boxes`` → ``box``; not ``address`` or
    ``status``. Deliberately rough and English only (``queries`` is not
    handled): the simplest rule that matches the common plurals, until
    embeddings replace the word comparison."""
    if len(word) < _MIN_PLURAL_WORD or word.endswith(("ss", "us", "is")):
        return word
    if _ES_PLURAL.search(word):
        return word[:-2]
    return word[:-1] if word.endswith("s") else word


def _is_content(word: str) -> bool:
    return len(word) >= _MIN_WORD and not word.isdigit()


def rarity(doc_freq: int, agents: int) -> float:
    """How much a feature that ``doc_freq`` of ``agents`` agents have weighs:
    smoothed inverse document frequency, 1 for a feature every agent has."""
    return math.log((agents + 1) / (doc_freq + 1)) + 1


@dataclass(frozen=True)
class Corpus:
    """How many agents have each feature of each signal."""

    agents: int
    tools: Counter[str]
    name_words: Counter[str]
    roles: Counter[str]

    @classmethod
    def of(cls, profiles: list[Profile]) -> Corpus:
        return cls(
            agents=len(profiles),
            tools=Counter(f for p in profiles for f in p.tools),
            name_words=Counter(f for p in profiles for f in p.name_words),
            roles=Counter(f for p in profiles for f in p.roles),
        )


def _similarity(
    a: frozenset[str], b: frozenset[str], doc_freq: Counter[str], agents: int
) -> float:
    """Weighted Jaccard of two feature sets, each feature weighed by its
    rarity: the weight both have over the weight either has, 0 when neither
    has any."""
    shared = sum(rarity(doc_freq[f], agents) for f in a & b)
    total = sum(rarity(doc_freq[f], agents) for f in a | b)
    return shared / total if total else 0.0


def compare(
    target: Profile, other: Profile, corpus: Corpus
) -> tuple[float, SimilarSignalsOut, SimilarReasonsOut]:
    """``other``'s score against ``target`` (0 to 1), the three similarities
    behind it, and what the two share, each sorted so the answer is stable.
    When the target has no roles (a project without a project policy, or an
    agent it has no section for) no candidate can share one, so the score is
    the weighted average of the other two for every candidate, not capped at
    0.9 and not dependent on whether a candidate has roles."""
    signals = SimilarSignalsOut(
        tools=_similarity(target.tools, other.tools, corpus.tools, corpus.agents),
        name_words=_similarity(
            target.name_words, other.name_words, corpus.name_words, corpus.agents
        ),
        roles=_similarity(target.roles, other.roles, corpus.roles, corpus.agents),
    )
    used = SIGNAL_WEIGHTS if target.roles else NO_ROLE_WEIGHTS
    score = sum(w * getattr(signals, name) for name, w in used.items()) / sum(
        used.values()
    )
    reasons = SimilarReasonsOut(
        shared_tools=sorted(target.tools & other.tools),
        shared_name_words=sorted(target.name_words & other.name_words),
        shared_roles=sorted(target.roles & other.roles),
    )
    return score, signals, reasons


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
    corpus = Corpus.of(list(profiles.values()))
    matches = []
    for name, profile in profiles.items():
        if name == agent:
            continue
        score, signals, reasons = compare(target, profile, corpus)
        score = round(score, 4)
        if score > 0:
            matches.append(
                SimilarAgentOut(
                    agent=name,
                    score=score,
                    has_policy=enforced[name].has_policy,
                    roles=sorted(profile.roles),
                    signals=signals,
                    reasons=reasons,
                )
            )
    matches.sort(key=lambda m: (-m.score, m.agent))
    return agent in manifests, matches[:limit]
