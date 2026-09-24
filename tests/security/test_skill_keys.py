"""Tests for the ``skill:`` policy key namespace (``security/models.py``).

Focus: building and recognising the three disclosure-level keys, and lowering a
``skills:`` block into :attr:`AgentPolicy.effective_tools` so both engines read it
through the same path as any tool. Nothing *decides* on a skill key yet — that is
M4 — so these pin the shape, not the verdict.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hexgate.security.models import (
    AgentPolicy,
    AgentTargetPolicy,
    BaseToolPolicy,
    SkillPolicy,
    is_skill_key,
    skill_key,
)


def test_skill_key_builds_each_prefix() -> None:
    assert skill_key("instructions", "refunder") == "skill:refunder"
    assert skill_key("resource", "refunder") == "skill.resource:refunder"
    assert skill_key("script", "refunder") == "skill.script:refunder"


def test_skill_key_canonicalises_the_name() -> None:
    """The adapter derives the same key from the runtime skill's name, so a padded
    authored name must normalize identically or the rule would never match."""
    assert skill_key("instructions", " refunder ") == "skill:refunder"


def test_is_skill_key_accepts_all_three_and_rejects_others() -> None:
    assert is_skill_key("skill:refunder")
    assert is_skill_key("skill.resource:refunder")
    assert is_skill_key("skill.script:refunder")
    # The separator is part of the prefix: an ordinary tool whose name merely
    # starts with the word is not a reserved key.
    assert not is_skill_key("skills")
    assert not is_skill_key("skill_lookup")
    assert not is_skill_key("agent.run")
    assert not is_skill_key("read_file")


def test_lowering_emits_one_key_per_via() -> None:
    policy = AgentPolicy(skills={"refunder": SkillPolicy(mode="allow")})

    assert sorted(policy.lowered_skill_tools()) == [
        "skill.resource:refunder",
        "skill.script:refunder",
        "skill:refunder",
    ]


def test_lowering_respects_a_narrowed_via() -> None:
    """A skill readable but not runnable lowers to the activation key alone."""
    policy = AgentPolicy(
        skills={"refunder": SkillPolicy(mode="allow", via=["instructions"])}
    )

    assert list(policy.lowered_skill_tools()) == ["skill:refunder"]


def test_lowered_value_is_the_skill_policy_instance() -> None:
    """The rule is reused, not rebuilt as a bare ``BaseToolPolicy`` — a rebuild
    would silently drop constraints, and any field later added to the base."""
    rule = SkillPolicy(mode="allow", constraints=["args.amount <= 500"])
    policy = AgentPolicy(skills={"refunder": rule})

    lowered = policy.lowered_skill_tools()["skill:refunder"]
    assert lowered is rule
    assert lowered.constraints == ["args.amount <= 500"]


def test_effective_tools_merges_tools_agents_and_skills() -> None:
    policy = AgentPolicy(
        tools={"read_file": BaseToolPolicy(mode="allow")},
        admission=BaseToolPolicy(mode="allow"),
        agents={"billing": AgentTargetPolicy(mode="allow", via=["tool"])},
        skills={"refunder": SkillPolicy(mode="allow", via=["instructions"])},
    )

    effective = policy.effective_tools
    assert effective["read_file"].mode == "allow"
    assert effective["agent.run"].mode == "allow"
    assert effective["agent.tool:billing"].mode == "allow"
    assert effective["skill:refunder"].mode == "allow"


def test_authored_skill_key_in_tools_is_rejected() -> None:
    """The namespace is reserved: an authored ``skill:`` tool would shadow, or be
    shadowed by, the lowered rule."""
    with pytest.raises(ValidationError, match="reserved"):
        AgentPolicy.model_validate({"tools": {"skill:refunder": {"mode": "allow"}}})


def test_authored_skill_key_is_accepted_on_a_resolved_policy() -> None:
    """A machine-resolved policy legitimately carries the lowered keys in ``tools``
    and must round-trip back through the loader (R-POL-002)."""
    policy = AgentPolicy.model_validate(
        {"tools": {"skill:refunder": {"mode": "allow"}}},
        context={"resolved": True},
    )

    assert policy.tools["skill:refunder"].mode == "allow"


def test_empty_via_is_rejected() -> None:
    with pytest.raises(ValidationError, match="via must list at least one"):
        SkillPolicy(mode="allow", via=[])
