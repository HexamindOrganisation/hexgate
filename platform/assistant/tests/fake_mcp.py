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


class _RecordingTool(Tool):
    _state: FakeMCP = PrivateAttr()

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        state = self._state
        headers = get_http_headers(include={"authorization"})
        state.calls.append(Call(self.name, arguments, headers.get("authorization")))
        await asyncio.sleep(state.delay)
        if state.refusal is not None:
            raise ToolError(state.refusal)
        return ToolResult(
            content=[TextContent(type="text", text=f"{self.name} result")]
        )


def _server(state: FakeMCP) -> FastMCP:
    # As the platform's server lists them: with $defs, not inlined.
    server = FastMCP("fake-hexgate-mcp", dereference_schemas=False)
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
