import asyncio
from contextlib import contextmanager
import os
from typing import Any, AsyncGenerator, Generator

import nest_asyncio
from google.adk.agents import BaseAgent
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types
from openinference.instrumentation.google_adk import GoogleADKInstrumentor

from langfuse import get_client, propagate_attributes

from fortify.user_context import UserContext
from fortify.adapters.google.wrapper import wrap_google_agent


class FortifyRunner:
    """Runner for Google ADK agents with Fortify tool policy and observability."""

    def __init__(
        self,
        *,
        agent: BaseAgent,
        app_name: str,
        session_service: BaseSessionService,
        api_key: str | None = None,
        **runner_kwargs: Any,
    ):
        self.api_key = api_key or os.getenv("FORTIFY_KEY")
        if self.api_key is None:
            raise ValueError(
                "FORTIFY_KEY is not set. Pass api_key= explicitly or set FORTIFY_KEY environment variable."
            )
        self._agent = agent
        self._app_name = app_name
        self._session_service = session_service
        self._runner_kwargs = runner_kwargs

    def _setup_observability(self):
        """Setup langfuse observability for the Google ADK agent."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: safe to patch (and only useful for sync entry points).
            # Patching a live loop breaks asyncio.current_task() on Python 3.12+.
            nest_asyncio.apply()
        get_client()
        GoogleADKInstrumentor().instrument()

    @contextmanager
    def _propagate(self, user_context: UserContext, agent_name: str):
        """Propagate the user context to the Langfuse trace/span"""
        kwargs: dict[str, Any] = {"tags": [f"google.runner.run.{agent_name}"]}
        kwargs["user_id"] = user_context.user_id
        kwargs["session_id"] = user_context.session_id
        kwargs["metadata"] = {"user_role": user_context.user_role}
        with propagate_attributes(**kwargs):
            yield

    def _build_runner(self, user_context: UserContext) -> Runner:
        """Build the Google ADK runner with the wrapped agent and the session service"""
        wrapped_agent = wrap_google_agent(self._agent, user_context, self.api_key)
        return Runner(
            agent=wrapped_agent,
            app_name=self._app_name,
            session_service=self._session_service,
            **self._runner_kwargs,
        )

    def run(
        self,
        *,
        new_message: types.Content,
        user_context: UserContext,
        **kwargs: Any,
    ) -> Generator[Any, None, None]:
        """Run the Google ADK agent synchronously, yielding events."""
        self._setup_observability()
        runner = self._build_runner(user_context)
        with self._propagate(user_context, self._agent.name):
            yield from runner.run(
                user_id=user_context.user_id,
                session_id=user_context.session_id,
                new_message=new_message,
                **kwargs,
            )

    async def run_async(
        self,
        *,
        new_message: types.Content | None = None,
        user_context: UserContext,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        """Run the Google ADK agent asynchronously, yielding events."""
        self._setup_observability()
        runner = self._build_runner(user_context)
        with self._propagate(user_context, self._agent.name):
            async for event in runner.run_async(
                user_id=user_context.user_id,
                session_id=user_context.session_id,
                new_message=new_message,
                **kwargs,
            ):
                yield event
