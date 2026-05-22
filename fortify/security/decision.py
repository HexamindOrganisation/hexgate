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
    """One policy decision for a proposed tool invocation."""

    outcome: DecisionOutcome
    agent_name: str
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
        """Default LLM-facing dict rendering. Adapters can build their own."""
        payload: dict[str, Any] = {
            "type": self.error_type or self.outcome.value,
            "message": self.reason,
            "tool_name": self.tool_name,
            "agent_name": self.agent_name,
            "retryable": False,
        }
        if self.role is not None:
            payload["role"] = self.role
        if self.hint is not None:
            payload["hint"] = self.hint
        return payload

    def as_error_message(self) -> str:
        """Default LLM-facing string rendering for adapters that return a
        tool-result string (OpenAI Agents, Google ADK) or raise a
        text-bearing exception (pydantic_ai's `ModelRetry`).
        """
        marker = self.error_type or self.outcome.value
        if self.outcome is DecisionOutcome.NEEDS_APPROVAL:
            body = (
                f"Tool '{self.tool_name}' requires human approval before execution"
            )
        else:
            body = f"Tool '{self.tool_name}' is denied by the agent policy"
        return f"[{marker}] {body}. The tool was not executed."

