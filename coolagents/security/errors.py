"""Security-related runtime errors."""

from __future__ import annotations


class PolicyDeniedError(RuntimeError):
    """Raise when a policy denies a tool invocation."""


class ApprovalRequiredError(RuntimeError):
    """Raise when a policy marks a tool invocation as approval-gated."""
