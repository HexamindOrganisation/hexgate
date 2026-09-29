"""Skill-gate wording and decision-arg format shared by every adapter.

One module so a pin written against ``content_hash`` and the model-facing denial
read alike whichever framework decided the skill; each adapter only wraps the
message in its own tool-result shape.
"""

from __future__ import annotations

from hexgate.security.decision import Decision, DecisionOutcome
from hexgate.security.models import SkillVia

CONTENT_HASH_PREFIX = "sha256:"

_HELD_ACTION_BY_VIA: dict[SkillVia, str] = {
    "instructions": "before it is loaded",
    "resource": "before its resource is read",
    "script": "before its script runs",
}


def skill_decision_hash(digest: str | None) -> str | None:
    """A body hexdigest as the ``content_hash`` decision arg; None stays None."""
    return f"{CONTENT_HASH_PREFIX}{digest}" if digest else None


def skill_denial_message(skill: str, via: SkillVia, decision: Decision) -> str:
    """Model-facing text for a denied or held skill.

    The closing sentence is deliberate: without it a model tends to improvise the
    procedure from memory, dropping exactly the guardrails the skill encoded.
    """
    if decision.outcome is DecisionOutcome.NEEDS_APPROVAL:
        body = f"skill {skill!r} requires human approval {_HELD_ACTION_BY_VIA[via]}"
    else:
        body = f"skill {skill!r} is not permitted by this agent's policy"
    return f"{body}. Do not attempt this task without it."
