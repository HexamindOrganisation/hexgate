"""Typed result of evaluating one proposed tool call against a PolicySet."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class DecisionOutcome(str, Enum):
    """Authorization outcome."""

    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True, slots=True)
class Decision:
    """One policy decision. ``arguments`` is exposed to host-side approval
    handlers but omitted from :meth:`as_error_payload` (the LLM already
    sent them)."""

    outcome: DecisionOutcome
    tool_name: str
    role: str | None = None
    reason: str = ""
    error_type: str | None = None
    hint: dict[str, Any] | None = None
    arguments: dict[str, Any] | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is DecisionOutcome.ALLOW

    def as_error_payload(self) -> dict[str, Any]:
        """Default LLM-facing rendering. Adapters can build their own."""
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
