"""The usage roots a policy decision reads, as one engine input.

One object instead of one keyword per root, so a new usage scope is a field here,
not a pass over every engine signature."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

AGENT_USAGE_ROOT: Final = "agent_usage"


@dataclass(frozen=True, slots=True)
class UsageInputs:
    """The ``agent_usage`` and ``user_usage`` namespaces for one decision.

    ``None`` means "not computed" (the policy references no path under that root);
    the engines then see an empty namespace, so a reference fails closed.
    ``user_usage`` is not exposed to the engines until its root is registered."""

    agent_usage: Mapping[str, Any] | None = None
    user_usage: Mapping[str, Any] | None = None

    def namespaces(self) -> dict[str, dict[str, Any]]:
        """The engine input roots, keyed by root name. Both engines build their
        input from this, so they can't disagree about which roots exist."""
        return {AGENT_USAGE_ROOT: dict(self.agent_usage or {})}


NO_USAGE: Final = UsageInputs()
