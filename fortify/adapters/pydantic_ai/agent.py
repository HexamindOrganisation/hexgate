"""Proxy around a pydantic_ai ``Agent`` for Fortify-aware calls."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

from langfuse import get_client, propagate_attributes
from pydantic_ai import Agent
from pydantic_ai.agent import AgentRun, AgentRunResult
from pydantic_ai.result import StreamedRunResult

from fortify.runtime import User


class FortifyPydanticAgent:
    """Proxy around a pydantic_ai ``Agent`` that opens a User scope per call.

    Policy enforcement is already installed on the wrapped tools at
    construction time (by :func:`wrap_pydantic_agent` calling
    :func:`~fortify.adapters.pydantic_ai.tools.wrap_tool`). This proxy is
    the call-time half: it pushes the active
    :class:`~fortify.runtime.User` onto the contextvar so the enforcer
    resolves the matching role, and it propagates user identity into
    every Langfuse trace/span emitted inside the call.

    ``user`` is supplied per invocation, not at construction — one
    wrapped agent can serve many users concurrently because the scope
    is per-call and contextvar-isolated.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        api_key: str,
        agent_name: str,
        tool_names: list[str],
    ) -> None:
        self._agent = agent
        self._api_key = api_key
        self._agent_name = agent_name
        self._tool_names = tool_names
        self._langfuse = get_client()
        self._setup_observability()

    def _setup_observability(self) -> None:
        """Setup tracing for the agents to be globally instrumented."""
        Agent.instrument_all()

    def _propagate_kwargs(self, user: User, method: str) -> dict[str, Any]:
        return {
            "tags": [f"pydantic_ai.agent.{method}"],
            "user_id": user.user_id,
            "session_id": user.session_id,
            "metadata": {"user_role": user.role},
        }

    @asynccontextmanager
    async def _abind(self, user: User, method: str) -> AsyncIterator[None]:
        """Open the User scope + Langfuse propagation for an async call."""
        async with user:
            with propagate_attributes(**self._propagate_kwargs(user, method)):
                yield

    @contextmanager
    def _bind(self, user: User, method: str) -> Iterator[None]:
        """Open the User scope + Langfuse propagation for a sync call."""
        with user.sync_scope():
            with propagate_attributes(**self._propagate_kwargs(user, method)):
                yield

    async def run(
        self,
        *args: Any,
        user: User,
        **kwargs: Any,
    ) -> AgentRunResult[Any]:
        """Run the agent asynchronously inside a User scope."""
        async with self._abind(user, "run"):
            return await self._agent.run(*args, **kwargs)

    def run_sync(
        self,
        *args: Any,
        user: User,
        **kwargs: Any,
    ) -> AgentRunResult[Any]:
        """Run the agent synchronously inside a User scope."""
        with self._bind(user, "run_sync"):
            return self._agent.run_sync(*args, **kwargs)

    @asynccontextmanager
    async def run_stream(
        self,
        *args: Any,
        user: User,
        **kwargs: Any,
    ) -> AsyncIterator[StreamedRunResult[Any, Any]]:
        """Stream the agent response asynchronously inside a User scope."""
        async with self._abind(user, "run_stream"):
            async with self._agent.run_stream(*args, **kwargs) as result:
                yield result

    @asynccontextmanager
    async def iter(
        self,
        *args: Any,
        user: User,
        **kwargs: Any,
    ) -> AsyncIterator[AgentRun[Any, Any]]:
        """Iterate over the agent execution graph asynchronously."""
        async with self._abind(user, "iter"):
            async with self._agent.iter(*args, **kwargs) as run:
                yield run

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the wrapped agent."""
        return getattr(self._agent, name)
