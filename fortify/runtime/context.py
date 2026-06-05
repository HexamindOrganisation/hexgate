"""Execution-time context — :class:`User` (per-invocation scope, read via
:func:`get_current_user` by all SDK adapters) and :class:`ToolUseContext`
(per-tool meta-argument carrying Biscuit-extracted facts).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, PrivateAttr

from fortify.runtime.workspace import Workspace


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


class User(BaseModel):
    """End-user scope for an agent invocation — async context manager.

    Use within a request handler to bind the agent to one user for the
    duration of a block. The agent runtime checks for an active User on
    each invocation, lazily mints a per-request Biscuit (signed by the
    platform-bound :class:`~fortify.cloud.FortifyClient`), and selects the
    role's policy from the agent's loaded set. The policy's per-tool
    ``constraints`` then evaluate against each call's arguments.

    Two invocation styles, same machinery underneath:

    * Ambient (FastAPI-friendly)::

          async with User(user_id="alice", role="billing"):
              async for event in stream_agent(agent, handler, input):
                  ...

    * Explicit (when contextvar inheritance is unreliable, e.g. you spawn
      a task without copying context)::

          ctx = ToolUseContext(biscuit_facts={"user": ["alice"]})
          async for event in stream_agent(
              agent, handler, input, tool_use_context=ctx
          ):
              ...

    The class is intentionally async-only — ``__aenter__`` / ``__aexit__``
    are cheap today but reserved for I/O later (audit emission on exit,
    KMS-backed signing on enter, JWKS freshness check, etc.). Sync callers
    can still wrap with ``asyncio.run(...)``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_id: str
    role: str | None = None
    session_id: str | None = None
    ttl_seconds: int | None = None

    # Stack so the same User instance survives nested ``async with`` blocks.
    _tokens: list[Any] = PrivateAttr(default_factory=list)

    async def __aenter__(self) -> "User":
        self._tokens.append(_CURRENT_USER.set(self))
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._tokens:
            _CURRENT_USER.reset(self._tokens.pop())

    @contextmanager
    def sync_scope(self) -> Iterator["User"]:
        """Sync mirror of ``async with self`` for sync entry points."""
        self._tokens.append(_CURRENT_USER.set(self))
        try:
            yield self
        finally:
            if self._tokens:
                _CURRENT_USER.reset(self._tokens.pop())


_CURRENT_USER: ContextVar[User | None] = ContextVar(
    "fortify_current_user",
    default=None,
)


def get_current_user() -> User | None:
    """Return the active :class:`User` for this execution flow, if any."""
    return _CURRENT_USER.get()
