from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator

from langchain_core.tools import BaseTool
from langchain_core.runnables import RunnableConfig
from langfuse import get_client
from langfuse.langchain import CallbackHandler
from langfuse import propagate_attributes
from langgraph.graph.state import CompiledStateGraph

from fortify.user_context import UserContext


class FortifyLangchainAgent:
    """
    Proxy around a CompiledStateGraph that instruments each execution
    method with a Langfuse CallbackHandler. User identity from the
    active UserContext is forwarded to every trace/span emitted
    inside the call via `langfuse.propagate_attributes`.
    """

    def __init__(
        self,
        *,
        agent: CompiledStateGraph,
        user_context: UserContext,
    ) -> None:
        self._agent = agent
        self._user_context = user_context
        self._langfuse = get_client()
        self._callback_handler = CallbackHandler()

    @contextmanager
    def _propagate(self, method: str) -> Iterator[None]:
        """Propagate the user context to the Langfuse trace/span"""
        ctx = self._user_context
        kwargs: dict[str, Any] = {"tags": [f"langchain.agent.{method}"]}
        if ctx is not None:
            kwargs["user_id"] = ctx.user_id
            kwargs["session_id"] = ctx.session_id
            kwargs["metadata"] = {"user_role": ctx.user_role}
        with propagate_attributes(**kwargs):
            yield

    def _with_callbacks(self, config: RunnableConfig | None) -> RunnableConfig:
        """Add the Fortify callback handler to the Langchain config"""
        merged: RunnableConfig = dict(config) if config else {}
        callbacks = list(merged.get("callbacks") or [])
        if self._callback_handler not in callbacks:
            callbacks.append(self._callback_handler)
        merged["callbacks"] = callbacks
        return merged

    async def ainvoke(
        self,
        input: dict[str, Any],
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the agent asynchronously"""
        with self._propagate("ainvoke"):
            return await self._agent.ainvoke(
                input, self._with_callbacks(config), **kwargs
            )

    def invoke(
        self,
        input: dict[str, Any],
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the agent synchronously"""
        with self._propagate("invoke"):
            return self._agent.invoke(input, self._with_callbacks(config), **kwargs)

    async def astream(
        self,
        input: dict[str, Any],
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the agent asynchronously"""
        with self._propagate("astream"):
            async for chunk in self._agent.astream(
                input, self._with_callbacks(config), **kwargs
            ):
                yield chunk

    def stream(
        self,
        input: dict[str, Any],
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """Stream the agent synchronously"""
        with self._propagate("stream"):
            yield from self._agent.stream(input, self._with_callbacks(config), **kwargs)

    async def astream_events(
        self,
        input: dict[str, Any],
        version: str,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the agent events asynchronously"""
        with self._propagate("astream_events"):
            async for event in self._agent.astream_events(
                input, version, config=self._with_callbacks(config), **kwargs
            ):
                yield event

    def __getattr__(self, name: str) -> Any:
        """Get the attribute from the agent"""
        return getattr(self._agent, name)
