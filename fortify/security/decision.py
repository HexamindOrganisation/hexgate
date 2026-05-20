"""Typed result of evaluating one proposed tool call against a PolicySet.

A :class:`Decision` is a pure value. The enforcer never executes a tool —
it returns one of these and stops. Adapters (LangChain, OpenAI, MCP, plain
callables) translate the decision into whatever their host expects;
host runtimes (``fortify --serve``, CLI loops, test harnesses) consume the
``NEEDS_APPROVAL`` signal and decide whether and how to resume.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class DecisionOutcome(str, Enum):
    """The shape of the authorization decision."""

    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True, slots=True)
class Decision:
    """One policy decision for a proposed tool invocation."""

    outcome: DecisionOutcome
    tool_name: str
    role: str | None = None
    reason: str = ""
    error_type: str | None = None
    hint: dict[str, Any] | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is DecisionOutcome.ALLOW

    def as_error_payload(self) -> dict[str, Any]:
        """Default rendering for LLM-facing tool results.

        Adapters that need a different shape should ignore this 
        and build their own from the Decision fields directly.
        """
        payload: dict[str, Any] = {
            "type": self.error_type or self.outcome.value,
            "message": self.reason,
            "tool_name": self.tool_name,
            "retryable": False,
        }
        if self.role is not None:
            payload["role"] = self.role
        if self.hint is not None:
            payload["hint"] = self.hint
        return payload
