"""Start-up configuration, read from the process environment only (prefix
``HEXGATE_ASSISTANT_``), never from the API's ``.env``."""

import os
from dataclasses import dataclass, field

# The port the server listens on, fixed here and not configurable.
PORT = 8090


@dataclass(frozen=True)
class Settings:
    # Loopback unless the environment says otherwise, so `make assistant-run`
    # is not reachable from the network. The image opts in to all interfaces.
    bind_host: str = "127.0.0.1"
    port: int = PORT
    # A Pydantic AI model string. None means no model: every turn is refused,
    # there is no default model to fall back on.
    model: str | None = None
    # Secret: kept out of repr, so logging the settings never prints it.
    openrouter_key: str | None = field(default=None, repr=False)
    # Where `make assistant-run` finds the MCP server of `make mcp-run`.
    mcp_url: str = "http://localhost:8080/mcp"

    @classmethod
    def from_env(cls) -> "Settings":
        # An empty value counts as unset: compose passes `KEY=` through as "".
        return cls(
            bind_host=os.environ.get("HEXGATE_ASSISTANT_BIND_HOST") or cls.bind_host,
            model=os.environ.get("HEXGATE_ASSISTANT_MODEL") or None,
            openrouter_key=os.environ.get("HEXGATE_ASSISTANT_OPENROUTER_KEY") or None,
            mcp_url=os.environ.get("HEXGATE_ASSISTANT_MCP_URL") or cls.mcp_url,
        )
