"""Thin async wrapper over the official ``mcp`` SDK's ``ClientSession``.

Exposes only the operations the proxy needs — ``list_tools`` (paginated)
and ``call_tool`` (timeout-bounded) — and owns the transport lifecycle
(open / close). Both stdio and streamable-HTTP transports are handled
behind one interface so :mod:`hexgate.mcp.proxy` doesn't branch on
transport at call time.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, Tool

from hexgate.mcp.config import MCPServerConfig

logger = logging.getLogger("hexgate.mcp.client")


class MCPConnectionError(RuntimeError):
    """Couldn't reach or initialize the MCP server.

    Wraps transport / handshake failures so callers don't have to know
    whether the underlying issue was a subprocess spawn error, an HTTP
    connect refused, or a JSON-RPC initialize failure.
    """


class MCPClient:
    """An open, initialized connection to one MCP server.

    Async context manager: ``async with MCPClient(config) as client:``
    handles transport setup, the JSON-RPC ``initialize`` handshake, and
    tear-down. Outside the ``with`` block the client raises — there's
    no half-open state to leak.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._exit_stack: contextlib.AsyncExitStack | None = None
        self._session: ClientSession | None = None

    @property
    def config(self) -> MCPServerConfig:
        return self._config

    async def __aenter__(self) -> "MCPClient":
        self._exit_stack = contextlib.AsyncExitStack()
        try:
            read, write = await self._open_transport(self._exit_stack)
            session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            self._session = session
        except BaseException as exc:
            # Tear down anything that was opened before the failure (stdio
            # subprocess in particular leaks otherwise) WITHOUT letting a
            # secondary error mask the real one. `raise … from exc` chains
            # the cause so the connect failure stays visible; suppress() on
            # the cleanup keeps a broken-pipe / cancelled-task from the
            # transport's __aexit__ from replacing the real reason.
            stack = self._exit_stack
            self._exit_stack = None
            self._session = None
            with contextlib.suppress(BaseException):
                if stack is not None:
                    await stack.aclose()
            if isinstance(exc, Exception):
                raise MCPConnectionError(
                    f"failed to connect to MCP server {self._config.name!r}: {exc}"
                ) from exc
            raise  # CancelledError / KeyboardInterrupt — propagate as-is
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        # Forward the active exception (if any) into the inner exit stack so
        # the transport's own __aexit__ takes the right cancellation path —
        # stdio in particular branches between "graceful close" and "kill"
        # based on whether it sees an exception.
        self._session = None
        stack = self._exit_stack
        self._exit_stack = None
        if stack is not None:
            await stack.__aexit__(exc_type, exc, tb)

    async def list_tools(self) -> list[Tool]:
        """Catalog of tools the server exposes.

        Walks ``nextCursor`` to completion so a server that paginates its
        ``tools/list`` response doesn't silently hide tools past page 1.
        """
        session = self._require_session()
        tools: list[Tool] = []
        cursor: str | None = None
        while True:
            result = await session.list_tools(cursor=cursor)
            tools.extend(result.tools)
            cursor = result.nextCursor
            if cursor is None:
                return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> CallToolResult:
        """Invoke an MCP tool by its server-local name.

        Bounded by ``call_timeout_seconds`` from the config — without
        this, a hung server would stall the entire agent loop.
        """
        session = self._require_session()
        timeout: timedelta | None = None
        if self._config.call_timeout_seconds is not None:
            timeout = timedelta(seconds=self._config.call_timeout_seconds)
        return await session.call_tool(
            name, arguments or {}, read_timeout_seconds=timeout
        )

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise MCPConnectionError(
                f"MCP client for {self._config.name!r} is not connected — "
                "use `async with MCPClient(config) as client:`"
            )
        return self._session

    async def _open_transport(
        self, stack: contextlib.AsyncExitStack
    ) -> tuple[Any, Any]:
        """Open the configured transport, return (read_stream, write_stream).

        Streamable-HTTP yields a third value (a session-id getter) we
        don't currently need; ignore it explicitly.
        """
        cfg = self._config
        if cfg.transport == "stdio":
            # cfg.env is None  → inherit parent env (SDK default)
            # cfg.env is {}    → run with an empty env (sandbox)
            # cfg.env is {...} → run with exactly these vars
            # The previous `dict(cfg.env) or None` collapsed {} to None,
            # turning an explicit sandbox request into full inheritance.
            params = StdioServerParameters(
                command=cfg.command or "",  # validated non-None by config
                args=list(cfg.args),
                env=None if cfg.env is None else dict(cfg.env),
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            return read, write
        # http
        read, write, _get_session_id = await stack.enter_async_context(
            streamablehttp_client(cfg.url or "", headers=cfg.headers or None)
        )
        return read, write
