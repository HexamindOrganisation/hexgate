"""Runner that wraps the OpenAI Agents ``Runner`` with Fortify policy + tracing.

Each entry point opens a :class:`~fortify.runtime.User` scope around the
delegated ``Runner.run*`` call so the wrapped tools' enforcers can resolve
the active role from the contextvar at call time. Langfuse propagation
mirrors the User identity into trace metadata.
"""

import asyncio
import os
from contextlib import contextmanager

import nest_asyncio
from agents import (
    Agent,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    RunState,
    TContext,
    TResponseInputItem,
)
from langfuse import get_client, propagate_attributes
from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

from fortify.adapters.openai.wrapper import wrap_openai_agent
from fortify.runtime import User


class FortifyRunner:
    """Runner for OpenAI agents with Fortify tool policy and observability."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("FORTIFY_KEY")
        if self.api_key is None:
            raise ValueError(
                "FORTIFY_KEY is not set. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
            )

    def _setup_observability(self):
        """Setup langfuse observability for the OpenAI agents."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: safe to patch (and only useful for sync entry points).
            # Patching a live loop breaks asyncio.current_task() on Python 3.12+.
            nest_asyncio.apply()
        get_client()
        OpenAIAgentsInstrumentor().instrument()

    @contextmanager
    def _propagate(self, user: User, agent_name: str):
        """Propagate the user identity to the Langfuse trace/span."""
        kwargs: dict[str, any] = {"tags": [f"openai.runner.run.{agent_name}"]}
        kwargs["user_id"] = user.user_id
        kwargs["session_id"] = user.session_id
        kwargs["metadata"] = {"user_role": user.role}
        with propagate_attributes(**kwargs):
            yield

    async def run(
        self,
        agent: Agent,
        input: str | list[TResponseInputItem] | RunState[TContext],
        user: User,
        run_config: RunConfig | None = None,
        **kwargs,
    ) -> RunResult:
        """Run the OpenAI agent asynchronously inside a User scope."""
        self._setup_observability()
        wrapped_agent = wrap_openai_agent(agent, api_key=self.api_key)
        async with user:
            with self._propagate(user, agent.name):
                return await Runner.run(
                    wrapped_agent, input, run_config=run_config, **kwargs
                )

    def run_sync(
        self,
        agent: Agent,
        input: str | list[TResponseInputItem] | RunState[TContext],
        user: User,
        run_config: RunConfig | None = None,
        **kwargs,
    ) -> RunResult:
        """Run the OpenAI agent synchronously inside a User scope."""
        self._setup_observability()
        wrapped_agent = wrap_openai_agent(agent, api_key=self.api_key)
        with user.sync_scope():
            with self._propagate(user, agent.name):
                return Runner.run_sync(
                    wrapped_agent, input, run_config=run_config, **kwargs
                )

    def run_streamed(
        self,
        agent: Agent,
        input: str | list[TResponseInputItem] | RunState[TContext],
        user: User,
        run_config: RunConfig | None = None,
        **kwargs,
    ) -> RunResultStreaming:
        """Run the OpenAI agent streamed asynchronously inside a User scope.

        ``Runner.run_streamed`` returns synchronously — it builds the run
        state and hands back a ``RunResultStreaming`` whose ``stream_events``
        async iterator is where tools actually run. The User scope is only
        needed during iteration, so it's opened inside the wrapped iterator;
        Langfuse propagation, in contrast, must be live for the setup call
        so the trace span attaches correctly.
        """
        self._setup_observability()
        wrapped_agent = wrap_openai_agent(agent, api_key=self.api_key)

        with self._propagate(user, agent.name):
            result = Runner.run_streamed(
                wrapped_agent, input, run_config=run_config, **kwargs
            )

        original_stream_events = result.stream_events

        async def _stream_events_with_scope():
            async with user:
                with self._propagate(user, agent.name):
                    async for event in original_stream_events():
                        yield event

        result.stream_events = _stream_events_with_scope
        return result
