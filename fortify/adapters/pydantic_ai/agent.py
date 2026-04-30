from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

from langfuse import get_client, propagate_attributes
from pydantic_ai import Agent
from pydantic_ai.agent import AgentRun, AgentRunResult
from pydantic_ai.result import StreamedRunResult

from fortify.user_context import UserContext


class FortifyPydanticAgent:
    """
    Proxy around a pydantic_ai `Agent` that propagates the active
    `UserContext` to every Langfuse trace/span emitted during a run
    via `langfuse.propagate_attributes`.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        user_context: UserContext,
    ) -> None:
        self._agent = agent
        self._user_context = user_context
        self._langfuse = get_client()
        self._setup_observability()
    
    def _setup_observability(self) -> None:
        """Setup tracing for the agents to be globally instrumented"""
        Agent.instrument_all()

    @contextmanager
    def _propagate(self, method: str) -> Iterator[None]:
        """Propagate the user context to the Langfuse trace/span"""
        ctx = self._user_context
        kwargs: dict[str, Any] = {"tags": [f"pydantic_ai.agent.{method}"]}
        if ctx is not None:
            kwargs["user_id"] = ctx.user_id
            kwargs["session_id"] = ctx.session_id
            kwargs["metadata"] = {"user_role": ctx.user_role}
        with propagate_attributes(**kwargs):
            yield

    async def run(self, *args: Any, **kwargs: Any) -> AgentRunResult[Any]:
        """Run the agent asynchronously"""
        with self._propagate("run"):
            return await self._agent.run(*args, **kwargs)

    def run_sync(self, *args: Any, **kwargs: Any) -> AgentRunResult[Any]:
        """Run the agent synchronously"""
        with self._propagate("run_sync"):
            return self._agent.run_sync(*args, **kwargs)

    @asynccontextmanager
    async def run_stream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[StreamedRunResult[Any, Any]]:
        """Stream the agent response asynchronously"""
        with self._propagate("run_stream"):
            async with self._agent.run_stream(*args, **kwargs) as result:
                yield result

    @asynccontextmanager
    async def iter(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[AgentRun[Any, Any]]:
        """Iterate over the agent execution graph asynchronously"""
        with self._propagate("iter"):
            async with self._agent.iter(*args, **kwargs) as run:
                yield run

    def __getattr__(self, name: str) -> Any:
        """Get the attribute from the agent"""
        return getattr(self._agent, name)
