"""Proxy around a pre-built ``CompiledStateGraph`` for Fortify-aware calls."""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator

from langchain_core.runnables import RunnableConfig
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler
from langgraph.graph.state import CompiledStateGraph

from fortify.runtime import User


class FortifyLangchainAgent:
    """Proxy around a ``CompiledStateGraph`` that opens a User scope per call.

    Policy enforcement is already installed on the wrapped tools at
    construction time (by :func:`wrap_langchain_agent` calling
    :func:`~fortify.adapters.langchain.tools.install_enforcer_on_tool`).
    This proxy is the call-time half: it pushes the active
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
        agent: CompiledStateGraph,
        api_key: str,
        tool_names: list[str],
    ) -> None:
        self._agent = agent
        self._api_key = api_key
        self._tool_names = tool_names
        self._langfuse = get_client()
        self._callback_handler = CallbackHandler()

    def _propagate_kwargs(self, user: User, method: str) -> dict[str, Any]:
        return {
            "tags": [f"langchain.agent.{method}"],
            "user_id": user.user_id,
            "session_id": user.session_id,
            "metadata": {"user_role": user.role},
        }

    def _with_callbacks(self, config: RunnableConfig | None) -> RunnableConfig:
        """Add the Fortify callback handler to the LangChain config."""
        merged: RunnableConfig = dict(config) if config else {}
        callbacks = list(merged.get("callbacks") or [])
        if self._callback_handler not in callbacks:
            callbacks.append(self._callback_handler)
        merged["callbacks"] = callbacks
        return merged

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        user: User,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the agent asynchronously inside a User scope."""
        async with user:
            with propagate_attributes(**self._propagate_kwargs(user, "ainvoke")):
                return await self._agent.ainvoke(
                    input, self._with_callbacks(config), **kwargs
                )

    def invoke(
        self,
        input: dict[str, Any],
        *,
        user: User,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the agent synchronously inside a User scope."""
        with user.sync_scope():
            with propagate_attributes(**self._propagate_kwargs(user, "invoke")):
                return self._agent.invoke(
                    input, self._with_callbacks(config), **kwargs
                )

    async def astream(
        self,
        input: dict[str, Any],
        *,
        user: User,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the agent asynchronously inside a User scope."""
        async with user:
            with propagate_attributes(**self._propagate_kwargs(user, "astream")):
                async for chunk in self._agent.astream(
                    input, self._with_callbacks(config), **kwargs
                ):
                    yield chunk

    def stream(
        self,
        input: dict[str, Any],
        *,
        user: User,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """Stream the agent synchronously inside a User scope."""
        with user.sync_scope():
            with propagate_attributes(**self._propagate_kwargs(user, "stream")):
                yield from self._agent.stream(
                    input, self._with_callbacks(config), **kwargs
                )

    async def astream_events(
        self,
        input: dict[str, Any],
        version: str,
        *,
        user: User,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the agent events asynchronously inside a User scope."""
        async with user:
            with propagate_attributes(**self._propagate_kwargs(user, "astream_events")):
                async for event in self._agent.astream_events(
                    input, version, config=self._with_callbacks(config), **kwargs
                ):
                    yield event

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the wrapped agent."""
        return getattr(self._agent, name)
