"""Proxy around a pre-built ``CompiledStateGraph`` for Hexgate-aware calls."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator, Literal

from langchain_core.runnables import RunnableConfig
from langfuse import get_client
from langfuse.langchain import CallbackHandler
from langgraph.graph.state import CompiledStateGraph

from hexgate.adapters._common import abind, bind
from hexgate.adapters.langchain.usage import HexgateUsageCallbackHandler
from hexgate.runtime import DEFAULT_AGENT_NAME, HexgateContext

if TYPE_CHECKING:
    from hexgate.security.bans import BanGate
    from hexgate.security.binding import PolicyBinding


class HexgateLangchainAgent:
    """Proxy around a ``CompiledStateGraph`` that opens a HexgateContext scope per call.

    Tools are already enforcer-installed at construction (by
    :func:`wrap_langchain_agent`). This proxy pushes the active
    :class:`HexgateContext` onto the contextvar and propagates identity into
    Langfuse spans. ``hexgate_context`` is per-call, so one proxy serves many
    users concurrently. When a policy binding is attached, every run
    method refreshes it first (fail-soft; 304 when unchanged).
    """

    def __init__(
        self,
        *,
        agent: CompiledStateGraph,
        api_key: str,
        tool_names: list[str],
        agent_name: str = DEFAULT_AGENT_NAME,
        binding: PolicyBinding | None = None,
        ban_gate: BanGate | None = None,
    ) -> None:
        self._agent = agent
        self._binding = binding
        self._ban_gate = ban_gate
        self._api_key = api_key
        self._tool_names = tool_names
        self._agent_name = agent_name
        self._langfuse = get_client()
        self._callback_handler = CallbackHandler()
        self._usage_handler = HexgateUsageCallbackHandler(
            agent_name=agent_name, api_key=api_key
        )

    async def _refresh_async(self) -> None:
        """Refresh the policy binding, if attached (async entry points)."""
        if self._binding is not None:
            await self._binding.refresh_async()

    def _refresh(self) -> None:
        """Refresh the policy binding, if one is attached (sync entry points)."""
        if self._binding is not None:
            self._binding.refresh()

    async def _check_ban_async(self, context: HexgateContext) -> None:
        """Refuse a banned agent/user before running, if a gate is attached."""
        if self._ban_gate is not None:
            await self._ban_gate.check_async(context)

    def _check_ban(self, context: HexgateContext) -> None:
        if self._ban_gate is not None:
            self._ban_gate.check(context)

    @asynccontextmanager
    async def _abind(self, context: HexgateContext, method: str) -> AsyncIterator[None]:
        """Async run boundary — identity scope, run facts, Langfuse propagation.
        See :func:`hexgate.adapters._common.abind`.

        The usage handler outlives the run (one instance per proxy, unlike the
        OpenAI adapter's per-run hooks), so the run's transcript cursor is
        dropped here on the way out. The key is read on the way *in*: the
        streaming entry points put this ``finally`` inside an async generator,
        which a consumer that breaks out early leaves to be finalized in a
        different ``Context`` — where re-reading it would name whichever run
        was bound at that moment rather than this one (see :meth:`end_run`).
        """
        async with abind(context, self._agent_name, self._tag(method)):
            turn_key = self._usage_handler.turn_key()
            try:
                yield
            finally:
                self._usage_handler.end_run(turn_key)

    @contextmanager
    def _bind(self, context: HexgateContext, method: str) -> Iterator[None]:
        """Sync mirror of :meth:`_abind`."""
        with bind(context, self._agent_name, self._tag(method)):
            turn_key = self._usage_handler.turn_key()
            try:
                yield
            finally:
                self._usage_handler.end_run(turn_key)

    @staticmethod
    def _tag(method: str) -> str:
        return f"langchain.agent.{method}"

    def _with_callbacks(self, config: RunnableConfig | None) -> RunnableConfig:
        """Append the Hexgate callback handlers to ``config['callbacks']``."""
        merged: RunnableConfig = dict(config) if config else {}
        callbacks = list(merged.get("callbacks") or [])
        for handler in (self._callback_handler, self._usage_handler):
            if handler not in callbacks:
                callbacks.append(handler)
        merged["callbacks"] = callbacks
        return merged

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        hexgate_context: HexgateContext,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the agent asynchronously inside a HexgateContext scope."""
        await self._refresh_async()
        await self._check_ban_async(hexgate_context)
        async with self._abind(hexgate_context, "ainvoke"):
            return await self._agent.ainvoke(
                input, self._with_callbacks(config), **kwargs
            )

    def invoke(
        self,
        input: dict[str, Any],
        *,
        hexgate_context: HexgateContext,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the agent synchronously inside a HexgateContext scope."""
        self._refresh()
        self._check_ban(hexgate_context)
        with self._bind(hexgate_context, "invoke"):
            return self._agent.invoke(input, self._with_callbacks(config), **kwargs)

    async def astream(
        self,
        input: dict[str, Any],
        *,
        hexgate_context: HexgateContext,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the agent asynchronously inside a HexgateContext scope."""
        await self._refresh_async()
        await self._check_ban_async(hexgate_context)
        async with self._abind(hexgate_context, "astream"):
            async for chunk in self._agent.astream(
                input, self._with_callbacks(config), **kwargs
            ):
                yield chunk

    def stream(
        self,
        input: dict[str, Any],
        *,
        hexgate_context: HexgateContext,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """Stream the agent synchronously inside a HexgateContext scope."""
        self._refresh()
        self._check_ban(hexgate_context)
        with self._bind(hexgate_context, "stream"):
            yield from self._agent.stream(input, self._with_callbacks(config), **kwargs)

    async def astream_events(
        self,
        input: dict[str, Any],
        *,
        hexgate_context: HexgateContext,
        config: RunnableConfig | None = None,
        version: Literal["v1", "v2"] = "v2",
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the agent events asynchronously inside a HexgateContext scope."""
        await self._refresh_async()
        await self._check_ban_async(hexgate_context)
        async with self._abind(hexgate_context, "astream_events"):
            async for event in self._agent.astream_events(
                input,
                config=self._with_callbacks(config),
                version=version,
                **kwargs,
            ):
                yield event

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the wrapped agent.

        Only the wrapped run methods (invoke/ainvoke/stream/astream/
        astream_events) enforce the ban gate + HexgateContext scope; methods reached
        here (batch, abatch, astream_log, …) bypass them.
        """
        return getattr(self._agent, name)
