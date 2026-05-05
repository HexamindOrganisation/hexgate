from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

from langfuse import get_client, propagate_attributes
from pydantic_ai import Agent
from pydantic_ai.agent import AgentRun, AgentRunResult
from pydantic_ai.result import StreamedRunResult

from fortify.user_context import UserContext
from fortify.adapters.pydantic_ai.policy import build_agent_policy
from fortify.adapters.pydantic_ai.tools import active_policy


class FortifyPydanticAgent:
    """
    Proxy around a pydantic_ai `Agent` that resolves the active Fortify
    `AgentPolicy` per invocation and propagates the caller's
    `UserContext` to every Langfuse trace/span emitted during a run.

    `user_context` is supplied per invocation, not at construction.
    Each call resolves the active policy and propagates the user
    identity to every Langfuse trace/span emitted inside the call.
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
        """Setup tracing for the agents to be globally instrumented"""
        Agent.instrument_all()

    @contextmanager
    def _bind(self, user_context: UserContext, method: str) -> Iterator[None]:
        """Resolve the per-call policy and propagate identity to traces."""
        policy = build_agent_policy(
            self._api_key,
            user_context,
            self._agent_name,
            self._tool_names,
        )
        attrs: dict[str, Any] = {
            "tags": [f"pydantic_ai.agent.{method}"],
            "user_id": user_context.user_id,
            "session_id": user_context.session_id,
            "metadata": {"user_role": user_context.user_role},
        }
        with propagate_attributes(**attrs), active_policy(policy):
            yield

    async def run(
        self,
        *args: Any,
        user_context: UserContext,
        **kwargs: Any,
    ) -> AgentRunResult[Any]:
        """Run the agent asynchronously"""
        with self._bind(user_context, "run"):
            return await self._agent.run(*args, **kwargs)

    def run_sync(
        self,
        *args: Any,
        user_context: UserContext,
        **kwargs: Any,
    ) -> AgentRunResult[Any]:
        """Run the agent synchronously"""
        with self._bind(user_context, "run_sync"):
            return self._agent.run_sync(*args, **kwargs)

    @asynccontextmanager
    async def run_stream(
        self,
        *args: Any,
        user_context: UserContext,
        **kwargs: Any,
    ) -> AsyncIterator[StreamedRunResult[Any, Any]]:
        """Stream the agent response asynchronously"""
        with self._bind(user_context, "run_stream"):
            async with self._agent.run_stream(*args, **kwargs) as result:
                yield result

    @asynccontextmanager
    async def iter(
        self,
        *args: Any,
        user_context: UserContext,
        **kwargs: Any,
    ) -> AsyncIterator[AgentRun[Any, Any]]:
        """Iterate over the agent execution graph asynchronously"""
        with self._bind(user_context, "iter"):
            async with self._agent.iter(*args, **kwargs) as run:
                yield run

    def __getattr__(self, name: str) -> Any:
        """Get the attribute from the agent"""
        return getattr(self._agent, name)
