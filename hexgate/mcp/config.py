"""Config + validation for an attached MCP server."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# server name → first segment of every qualified tool. Lowercase + digits +
# hyphens, max 32 chars: keeps qualified names within OpenAI's 64-char
# tool-name limit even for medium-long MCP tool names.
_SERVER_NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")

Transport = Literal["stdio", "http"]


class MCPServerConfigError(ValueError):
    """Invalid MCP server config (caller mistake, raised at construction)."""


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Description of an MCP server to attach to an agent.

    Two transports — ``stdio`` for local subprocess servers
    (``command`` + ``args`` + ``env``), ``http`` for remote streamable-
    HTTP endpoints (``url`` + ``headers``). One of those two field sets
    must match the chosen transport; mixing raises at construction.
    """

    name: str
    transport: Transport
    # stdio fields — required when transport="stdio".
    command: str | None = None
    args: tuple[str, ...] = ()
    # ``None`` = inherit the parent process env (the SDK default; convenient
    # for dev, BUT exposes PATH / AWS_* / etc. to the MCP subprocess).
    # An explicit dict (including an empty ``{}``) is passed through verbatim
    # — set ``env={}`` to sandbox the subprocess to an empty environment, or
    # ``env={"SLACK_TOKEN": ...}`` to forward only the variables you need.
    env: dict[str, str] | None = None
    # http fields — required when transport="http".
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # Per-call timeout for tool invocations. ``None`` = no bound (the SDK's
    # default), which lets a wedged MCP server stall the agent indefinitely.
    # 30s is generous for most network/disk tools and short enough that the
    # agent loop can recover within a single retry budget.
    call_timeout_seconds: float | None = 30.0

    def __post_init__(self) -> None:
        if not _SERVER_NAME_RE.match(self.name):
            raise MCPServerConfigError(
                f"MCP server name {self.name!r} must match {_SERVER_NAME_RE.pattern} "
                "— lowercase letters, digits, hyphens; ≤32 chars. The name becomes "
                "the namespace in qualified tool names (mcp-<name>-<tool>) which "
                "must stay under OpenAI's 64-char tool-name limit."
            )
        if self.transport == "stdio":
            if not self.command:
                raise MCPServerConfigError(
                    f"MCP server {self.name!r}: transport=stdio requires `command`."
                )
            if self.url is not None:
                raise MCPServerConfigError(
                    f"MCP server {self.name!r}: transport=stdio takes `command` "
                    "+ `args` + `env`, not `url` — pick one transport."
                )
        elif self.transport == "http":
            if not self.url:
                raise MCPServerConfigError(
                    f"MCP server {self.name!r}: transport=http requires `url`."
                )
            if self.command is not None:
                raise MCPServerConfigError(
                    f"MCP server {self.name!r}: transport=http takes `url` + "
                    "`headers`, not `command` — pick one transport."
                )
        else:
            raise MCPServerConfigError(
                f"MCP server {self.name!r}: unknown transport {self.transport!r} "
                "— supported: 'stdio', 'http'."
            )

    def qualified_tool_name(self, tool_name: str) -> str:
        """Build the runtime tool name from this server + the MCP tool name.

        Hyphens between segments (not colons) so OpenAI Function Calling
        accepts the name. Caller is responsible for the inner tool name
        already being OpenAI-valid; MCP spec recommends snake_case which
        passes the same character class.
        """
        return f"mcp-{self.name}-{tool_name}"
