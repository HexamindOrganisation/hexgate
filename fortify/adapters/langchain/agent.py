from __future__ import annotations

from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator

from langchain_core.runnables import RunnableConfig
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler
from langgraph.graph.state import CompiledStateGraph

from fortify.user_context import UserContext
from fortify.adapters.langchain.policy import build_agent_policy
from fortify.adapters.langchain.tools import active_policy


class FortifyLangchainAgent:
    """
    Proxy around a `CompiledStateGraph` that instruments each execution
    method with a Langfuse `CallbackHandler` and a per-call Fortify
    `AgentPolicy`.

    `user_context` is supplied per invocation, not at construction.
    Each call resolves the active policy and propagates the user
    identity to every Langfuse trace/span emitted inside the call.
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

    @contextmanager
    def _bind(self, user_context: UserContext, method: str) -> Iterator[None]:
        """Resolve the per-call policy and propagate identity to traces."""
        policy = build_agent_policy(
            self._api_key,
            user_context,
            getattr(self._agent, "name", "default"),
            self._tool_names,
        )
        attrs: dict[str, Any] = {
            "tags": [f"langchain.agent.{method}"],
            "user_id": user_context.user_id,
            "session_id": user_context.session_id,
            "metadata": {"user_role": user_context.user_role},
        }
        with propagate_attributes(**attrs), active_policy(policy):
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
        *,
        user_context: UserContext,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the agent asynchronously"""
        with self._bind(user_context, "ainvoke"):
            return await self._agent.ainvoke(
                input, self._with_callbacks(config), **kwargs
            )

    def invoke(
        self,
        input: dict[str, Any],
        *,
        user_context: UserContext,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the agent synchronously"""
        with self._bind(user_context, "invoke"):
            return self._agent.invoke(input, self._with_callbacks(config), **kwargs)

    async def astream(
        self,
        input: dict[str, Any],
        *,
        user_context: UserContext,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the agent asynchronously"""
        with self._bind(user_context, "astream"):
            async for chunk in self._agent.astream(
                input, self._with_callbacks(config), **kwargs
            ):
                yield chunk

    def stream(
        self,
        input: dict[str, Any],
        *,
        user_context: UserContext,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """Stream the agent synchronously"""
        with self._bind(user_context, "stream"):
            yield from self._agent.stream(input, self._with_callbacks(config), **kwargs)

    async def astream_events(
        self,
        input: dict[str, Any],
        version: str,
        *,
        user_context: UserContext,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the agent events asynchronously"""
        with self._bind(user_context, "astream_events"):
            async for event in self._agent.astream_events(
                input, version, config=self._with_callbacks(config), **kwargs
            ):
                yield event

    def __getattr__(self, name: str) -> Any:
        """Get the attribute from the agent"""
        return getattr(self._agent, name)
