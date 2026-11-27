"""A stand-in for the platform's MCP server, served over real HTTP.

It lists the tools of ``mcp_tools.json`` (the platform's own definitions)
plus the ones the assistant must never be offered, and records every call
with the bearer token it came with.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.tests import run_server_async
from mcp.types import TextContent
from pydantic import PrivateAttr

SNAPSHOT = Path(__file__).parent / "mcp_tools.json"

# On the server, never on the allowlist.
OFF_ALLOWLIST = ("policy_apply", "policy_save", "projects_list")


def snapshot_tools() -> list[dict[str, Any]]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@dataclass
class Call:
    tool: str
    arguments: dict[str, Any]
    authorization: str | None


@dataclass
class FakeMCP:
    url: str = ""
    calls: list[Call] = field(default_factory=list)
    # Seconds every tool call sleeps before it answers.
    delay: float = 0.0
    # Every tool call is refused with this message, as the server refuses one.
    refusal: str | None = None
    # Per tool: the text it answers instead of "<name> result", and seconds
    # it sleeps on top of `delay`.
    results: dict[str, str] = field(default_factory=dict)
    delays: dict[str, float] = field(default_factory=dict)
    # Per tool: the message it is refused with, after its sleep, as the
    # platform's server words it (see `_RecordingTool.run`).
    errors: dict[str, str] = field(default_factory=dict)
    # Every request's method, in order, as the server received them.
    methods: list[str] = field(default_factory=list)
    # tools/list is refused while this is set, and answers after this many
    # seconds.
    refuse_listing: bool = False
    list_delay: float = 0.0


class _RecordingTool(Tool):
    _state: FakeMCP = PrivateAttr()

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        state = self._state
        headers = get_http_headers(include={"authorization"})
        state.calls.append(Call(self.name, arguments, headers.get("authorization")))
        await asyncio.sleep(state.delay + state.delays.get(self.name, 0))
        if self.name in state.errors:
            # The platform's server is the official SDK's FastMCP, which
            # rewraps every error a tool raises with this prefix.
            raise ToolError(
                f"Error executing tool {self.name}: {state.errors[self.name]}"
            )
        if state.refusal is not None:
            raise ToolError(state.refusal)
        text = state.results.get(self.name, f"{self.name} result")
        return ToolResult(content=[TextContent(type="text", text=text)])


class _Recording(Middleware):
    def __init__(self, state: FakeMCP) -> None:
        self._state = state

    async def on_request(self, context: MiddlewareContext, call_next):
        self._state.methods.append(context.method or "")
        if context.method == "tools/list":
            await asyncio.sleep(self._state.list_delay)
            if self._state.refuse_listing:
                raise ToolError("listing refused")
        return await call_next(context)


def _server(state: FakeMCP) -> FastMCP:
    # As the platform's server lists them: with $defs, not inlined.
    server = FastMCP("fake-hexgate-mcp", dereference_schemas=False)
    server.add_middleware(_Recording(state))
    definitions = snapshot_tools() + [
        {"name": n, "description": n, "inputSchema": {"type": "object"}}
        for n in OFF_ALLOWLIST
    ]
    for d in definitions:
        tool = _RecordingTool(
            name=d["name"], description=d["description"], parameters=d["inputSchema"]
        )
        tool._state = state
        server.add_tool(tool)
    return server


@asynccontextmanager
async def serve() -> AsyncIterator[FakeMCP]:
    state = FakeMCP()
    async with run_server_async(_server(state)) as url:
        state.url = url
        yield state
