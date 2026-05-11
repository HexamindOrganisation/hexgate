"""Execution-time context propagation — tool-scope and user-scope."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

from pydantic import BaseModel

from fortify.runtime.workspace import Workspace


class UserContext(BaseModel):
    """Per-invocation user identity propagated into traces and policy decisions."""

    user_id: str
    session_id: str
    user_role: str


@dataclass(slots=True)
class ToolUseContext:
    """Runtime context injected into tools as a hidden meta-argument.

    ``biscuit_facts`` carries the single-arity facts the SDK extracted from
    a verified Biscuit envelope — ``user``, ``scope``, numeric limits, etc.
    The policy engine reads them through this context so callers don't have
    to thread facts down to each tool by hand. ``None`` means *no token
    facts present* (local-only flows); ``{}`` means *facts checked but
    nothing extracted*.
    """

    workspace: Workspace | None = None
    agent_name: str | None = None
    biscuit_facts: dict[str, list[str | int]] | None = None


_CURRENT_TOOL_USE_CONTEXT: ContextVar[ToolUseContext | None] = ContextVar(
    "fortify_tool_use_context",
    default=None,
)


def get_current_tool_use_context() -> ToolUseContext | None:
    """Return the current runtime tool context, when one is active."""
    return _CURRENT_TOOL_USE_CONTEXT.get()


def set_current_tool_use_context(
    context: ToolUseContext,
) -> Token[ToolUseContext | None]:
    """Install a tool-use context for the current execution flow."""
    return _CURRENT_TOOL_USE_CONTEXT.set(context)


def reset_current_tool_use_context(token: Token[ToolUseContext | None]) -> None:
    """Restore the previous tool-use context after a run completes."""
    _CURRENT_TOOL_USE_CONTEXT.reset(token)
