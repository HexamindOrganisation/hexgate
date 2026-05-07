import asyncio
from contextlib import contextmanager
import os
from agents import (
    RunResult,
    RunResultStreaming,
    RunState,
    Runner,
    Agent,
    RunConfig,
    TContext,
    TResponseInputItem,
)
import nest_asyncio
from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

from langfuse import get_client, propagate_attributes

from fortify.runtime import UserContext
from fortify.adapters.openai.wrapper import wrap_openai_agent


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
    def _propagate(self, user_context: UserContext, agent_name: str):
        """Propagate the user context to the Langfuse trace/span"""
        kwargs: dict[str, any] = {"tags": [f"openai.runner.run.{agent_name}"]}
        kwargs["user_id"] = user_context.user_id
        kwargs["session_id"] = user_context.session_id
        kwargs["metadata"] = {"user_role": user_context.user_role}
        with propagate_attributes(**kwargs):
            yield

    async def run(
        self,
        agent: Agent,
        input: str | list[TResponseInputItem] | RunState[TContext],
        user_context: UserContext,
        run_config: RunConfig | None = None,
        **kwargs,
    ) -> RunResult:
        """Run the OpenAI agent asynchronously"""
        self._setup_observability()
        wrapped_agent = wrap_openai_agent(agent, user_context, self.api_key)
        with self._propagate(user_context, agent.name):
            return await Runner.run(
                wrapped_agent, input, run_config=run_config, **kwargs
            )

    def run_sync(
        self,
        agent: Agent,
        input: str | list[TResponseInputItem] | RunState[TContext],
        user_context: UserContext,
        run_config: RunConfig | None = None,
        **kwargs,
    ) -> RunResult:
        """Run the OpenAI agent synchronously"""
        self._setup_observability()
        wrapped_agent = wrap_openai_agent(agent, user_context, self.api_key)
        with self._propagate(user_context, agent.name):
            return Runner.run_sync(
                wrapped_agent, input, run_config=run_config, **kwargs
            )

    def run_streamed(
        self,
        agent: Agent,
        input: str | list[TResponseInputItem] | RunState[TContext],
        user_context: UserContext,
        run_config: RunConfig | None = None,
        **kwargs,
    ) -> RunResultStreaming:
        """Run the OpenAI agent streamed asynchronously"""
        self._setup_observability()
        wrapped_agent = wrap_openai_agent(agent, user_context, self.api_key)

        with self._propagate(user_context, agent.name):
            result = Runner.run_streamed(
                wrapped_agent, input, run_config=run_config, **kwargs
            )

        # Runner.run_streamed returns synchronously: wrap the iterator so propagation is
        # re-entered for the lifetime of the stream.
        original_stream_events = result.stream_events

        async def _stream_events_with_propagation():
            with self._propagate(user_context, agent.name):
                async for event in original_stream_events():
                    yield event

        result.stream_events = _stream_events_with_propagation
        return result
