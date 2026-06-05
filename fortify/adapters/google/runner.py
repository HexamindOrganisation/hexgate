"""Google ADK ``Runner`` wrapper: opens a :class:`User` scope around each
``Runner.run*`` call so the wrapped tools' enforcers can resolve the
active role. Langfuse propagation mirrors User identity into spans.
"""

import asyncio
import os
from contextlib import contextmanager
from typing import Any, AsyncGenerator, Generator

import nest_asyncio
from google.adk.agents import BaseAgent
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types
from langfuse import get_client, propagate_attributes
from openinference.instrumentation.google_adk import GoogleADKInstrumentor

from fortify.adapters.google.wrapper import wrap_google_agent
from fortify.runtime import User


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
        # Policy resolves at construction (platform pull + verify — the
        # loud-failure point for bad keys / signatures / unreachable
        # platform); the Runner is built once and reused since role
        # resolution happens at call time via the User contextvar. The
        # binding refreshes at the top of every run — a policy change
        # hot-swaps via the shared enforcer without touching this Runner
        # or the cloned agent.
        self._wrapped_agent, self._binding = wrap_google_agent(
            agent, api_key=self.api_key
        )
        self._runner = Runner(
            agent=self._wrapped_agent,
            app_name=app_name,
            session_service=session_service,
            **runner_kwargs,
        )
        self._agent_name = getattr(agent, "name", "default")

    def _setup_observability(self):
        """Install Langfuse + GoogleADKInstrumentor (idempotent)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: safe to patch (and only useful for sync entry points).
            # Patching a live loop breaks asyncio.current_task() on Python 3.12+.
            nest_asyncio.apply()
        get_client()
        GoogleADKInstrumentor().instrument()

    @contextmanager
    def _propagate(self, user: User):
        """Propagate User identity into Langfuse spans for the block."""
        kwargs: dict[str, Any] = {"tags": [f"google.runner.run.{self._agent_name}"]}
        kwargs["user_id"] = user.user_id
        kwargs["session_id"] = user.session_id
        kwargs["metadata"] = {"user_role": user.role}
        with propagate_attributes(**kwargs):
            yield

    def run(
        self,
        *,
        new_message: types.Content,
        user: User,
        **kwargs: Any,
    ) -> Generator[Any, None, None]:
        """Run the Google ADK agent synchronously, yielding events."""
        self._setup_observability()
        self._binding.refresh()  # per-run policy pull; 304 when unchanged
        with user.sync_scope(), self._propagate(user):
            yield from self._runner.run(
                user_id=user.user_id,
                session_id=user.session_id,
                new_message=new_message,
                **kwargs,
            )

    async def run_async(
        self,
        *,
        new_message: types.Content | None = None,
        user: User,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        """Run the Google ADK agent asynchronously, yielding events."""
        self._setup_observability()
        await self._binding.refresh_async()  # per-run policy pull; 304 when unchanged
        async with user:
            with self._propagate(user):
                async for event in self._runner.run_async(
                    user_id=user.user_id,
                    session_id=user.session_id,
                    new_message=new_message,
                    **kwargs,
                ):
                    yield event
