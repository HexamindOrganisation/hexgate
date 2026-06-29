"""Tests for the MCP → LangChain tool proxy layer.

Uses a hand-rolled fake :class:`MCPClient` to exercise the proxy without
spawning a real subprocess. The end-to-end transport (stdio + http) is
covered by the official ``mcp`` SDK's own tests; what we own here is the
qualified-naming, schema passthrough, call-forwarding, envelope shape,
structured-output handling, schema validation, and lifecycle behavior
of our wrapper.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import (
    CallToolResult,
    EmbeddedResource,
    TextContent,
    TextResourceContents,
    Tool,
)

from hexgate.mcp import MCPServerConfig, MCPToolset
from hexgate.mcp.client import MCPConnectionError
from hexgate.mcp.proxy import (
    _build_proxy_tool,
    _result_to_envelope,
    _ToolsetState,
)


# ---- helpers ---------------------------------------------------------------


def _text_result(text: str, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)], isError=is_error
    )


class _FakeMCPClient:
    """Minimal stand-in for :class:`MCPClient` — records calls, scripts results.

    Implements just the subset _build_proxy_tool reads: ``config`` (for the
    qualified name) and ``call_tool``. Each call appends to ``self.calls``.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_result: CallToolResult | Exception = _text_result("default")

    def returns(self, result: CallToolResult) -> None:
        self._next_result = result

    def raises(self, exc: Exception) -> None:
        self._next_result = exc

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self.calls.append((name, arguments))
        if isinstance(self._next_result, Exception):
            raise self._next_result
        return self._next_result


def _slack_config(**overrides: Any) -> MCPServerConfig:
    base: dict[str, Any] = {
        "name": "slack",
        "transport": "stdio",
        "command": "slack-mcp",
    }
    base.update(overrides)
    return MCPServerConfig(**base)


def _tool(name: str, *, description: str = "", schema: dict | None = None) -> Tool:
    return Tool(
        name=name,
        description=description or None,
        inputSchema=schema or {"type": "object", "properties": {}},
    )


def _state(client: _FakeMCPClient | Any) -> _ToolsetState:
    """Build the proxy state shim the proxy closure consumes."""
    return _ToolsetState(client)  # type: ignore[arg-type]


# ---- _result_to_envelope ---------------------------------------------------


def test_envelope_wraps_text_content_as_ok() -> None:
    """Native @agent_tool returns {"ok": True, "content": ...} — MCP proxy
    must match so callers can discriminate uniformly across origins."""
    result = CallToolResult(
        content=[
            TextContent(type="text", text="line one"),
            TextContent(type="text", text="line two"),
        ],
        isError=False,
    )
    out = _result_to_envelope("mcp-slack-search", result)
    assert out == {"ok": True, "content": "line one\nline two"}


def test_envelope_marks_isError_as_not_ok() -> None:
    """isError must flip the envelope to {"ok": False, ...} — otherwise the
    agent would treat a failed tool call as successful."""
    out = _result_to_envelope(
        "mcp-slack-search", _text_result("rate limited", is_error=True)
    )
    assert out["ok"] is False
    assert out["error"]["type"] == "tool_error"
    assert "rate limited" in out["error"]["message"]
    assert out["error"]["tool_name"] == "mcp-slack-search"


def test_envelope_includes_structured_content() -> None:
    """MCP's structuredContent is a first-class typed return path — must not
    silently disappear (was finding #4 in the code review)."""
    result = CallToolResult(
        content=[],
        structuredContent={"channel_id": "C123", "ts": "1700000000.000"},
        isError=False,
    )
    out = _result_to_envelope("mcp-slack-send", result)
    assert out["ok"] is True
    # Rendered as JSON text the LLM can parse.
    assert '"channel_id": "C123"' in out["content"]
    assert '"ts": "1700000000.000"' in out["content"]


def test_envelope_extracts_text_from_embedded_resource() -> None:
    """EmbeddedResource carries readable text via ``.resource.text``;
    dropping it was a silent data loss."""
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri="file:///tmp/note.txt", text="hello from a resource"
                ),
            )
        ],
        isError=False,
    )
    out = _result_to_envelope("mcp-fs-read", result)
    assert out["ok"] is True
    assert "hello from a resource" in out["content"]


def test_envelope_falls_back_to_placeholder_for_empty_content() -> None:
    """A tool that returns nothing shouldn't render as the literal empty
    string — that would look like a "" success to the LLM."""
    empty = CallToolResult(content=[], isError=False)
    out = _result_to_envelope("mcp-slack-noop", empty)
    assert out == {"ok": True, "content": "(no textual content)"}


# ---- _build_proxy_tool — naming + schema + description ---------------------


def test_proxy_tool_uses_qualified_name() -> None:
    """LLM-visible name must be ``mcp-<server>-<tool>`` so it can't collide
    with native tools or with another MCP server exposing the same tool."""
    proxy = _build_proxy_tool(
        _state(_FakeMCPClient(_slack_config())), _slack_config(), _tool("send_message")
    )
    assert proxy.name == "mcp-slack-send_message"


def test_proxy_tool_passes_through_description_and_schema() -> None:
    """MCP's description + inputSchema must reach the LLM unchanged so it
    knows when + how to call the tool."""
    schema = {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "Slack channel ID"},
            "text": {"type": "string"},
        },
        "required": ["channel", "text"],
    }
    cfg = _slack_config()
    proxy = _build_proxy_tool(
        _state(_FakeMCPClient(cfg)),
        cfg,
        _tool("send_message", description="Post a message to a channel", schema=schema),
    )
    assert proxy.description == "Post a message to a channel"
    assert proxy.args_schema == schema


def test_proxy_tool_falls_back_to_default_description() -> None:
    """A tool with no description shouldn't break the LangChain BaseTool
    contract (which requires a non-empty description) — fall back to the
    qualified name so the LLM at least sees a label."""
    cfg = _slack_config()
    proxy = _build_proxy_tool(_state(_FakeMCPClient(cfg)), cfg, _tool("list_channels"))
    assert proxy.description
    assert "mcp-slack-list_channels" in proxy.description


# ---- proxy call forwarding -------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_forwards_call_with_server_local_name() -> None:
    """The proxy must call ``client.call_tool(inner_name, ...)`` — NOT the
    qualified name — because the server only knows its local tool names."""
    client = _FakeMCPClient(_slack_config())
    client.returns(_text_result("ok"))
    proxy = _build_proxy_tool(_state(client), _slack_config(), _tool("send_message"))

    result = await proxy.ainvoke({"channel": "#dev", "text": "hi"})

    assert client.calls == [("send_message", {"channel": "#dev", "text": "hi"})]
    assert result == {"ok": True, "content": "ok"}


@pytest.mark.asyncio
async def test_proxy_returns_error_envelope_on_provider_exception() -> None:
    """Provider RuntimeErrors (e.g. SDK output-schema validation failures)
    must surface as a {"ok": False, "error": ...} envelope — never bubble
    up and abort the agent run (was finding #7)."""
    client = _FakeMCPClient(_slack_config())
    client.raises(RuntimeError("simulated SDK output-schema violation"))
    proxy = _build_proxy_tool(_state(client), _slack_config(), _tool("send_message"))

    result = await proxy.ainvoke({"channel": "#dev", "text": "hi"})

    assert result["ok"] is False
    assert "simulated SDK output-schema violation" in result["error"]["message"]
    assert result["error"]["tool_name"] == "mcp-slack-send_message"


@pytest.mark.asyncio
async def test_proxy_returns_error_envelope_on_not_connected() -> None:
    """An MCPConnectionError (use-after-close at the client level) must
    also produce an envelope — never raise out of the proxy."""
    client = _FakeMCPClient(_slack_config())
    client.raises(MCPConnectionError("session torn down"))
    proxy = _build_proxy_tool(_state(client), _slack_config(), _tool("send_message"))

    result = await proxy.ainvoke({"channel": "#dev", "text": "hi"})

    assert result["ok"] is False
    assert result["error"]["type"] == "not_connected"


# ---- schema validation -----------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_rejects_missing_required_arg_before_round_trip() -> None:
    """An LLM call that omits a required arg must NOT reach the server —
    return a structured validation error envelope instead (was finding #13)."""
    schema = {
        "type": "object",
        "properties": {
            "channel": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["channel", "text"],
    }
    client = _FakeMCPClient(_slack_config())
    proxy = _build_proxy_tool(
        _state(client), _slack_config(), _tool("send_message", schema=schema)
    )

    result = await proxy.ainvoke({"channel": "#dev"})  # missing "text"

    assert result["ok"] is False
    assert result["error"]["type"] == "schema_validation_error"
    # The server was never called.
    assert client.calls == []


@pytest.mark.asyncio
async def test_proxy_accepts_valid_args_through_schema_validation() -> None:
    """Schema validation must be opt-in to passing args — once they match
    the inputSchema, the proxy forwards as usual."""
    schema = {
        "type": "object",
        "properties": {"channel": {"type": "string"}},
        "required": ["channel"],
    }
    client = _FakeMCPClient(_slack_config())
    client.returns(_text_result("sent"))
    proxy = _build_proxy_tool(
        _state(client), _slack_config(), _tool("send_message", schema=schema)
    )

    result = await proxy.ainvoke({"channel": "#dev"})

    assert client.calls == [("send_message", {"channel": "#dev"})]
    assert result == {"ok": True, "content": "sent"}


# ---- use-after-close guard -------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_post_close_returns_clear_error_envelope() -> None:
    """If the toolset has been torn down, the proxy must NOT raise the
    cryptic 'use async with MCPClient(...)' error from the underlying
    client — the user never instantiated an MCPClient (they used
    MCPToolset). Was finding #10."""
    state = _state(_FakeMCPClient(_slack_config()))
    proxy = _build_proxy_tool(state, _slack_config(), _tool("send_message"))

    # Simulate the toolset's __aexit__ marking the state closed.
    state.open = False

    result = await proxy.ainvoke({"channel": "#dev", "text": "hi"})

    assert result["ok"] is False
    assert result["error"]["type"] == "use_after_close"
    # Must point at MCPToolset specifically, not at MCPClient.
    assert "MCPToolset" in result["error"]["message"]


# ---- MCPToolset construction + dedup ---------------------------------------


def test_toolset_requires_at_least_one_config() -> None:
    with pytest.raises(ValueError, match="at least one"):
        MCPToolset()


def test_toolset_rejects_duplicate_server_names() -> None:
    """OpenAI's function-calling API rejects duplicate function names —
    catch the construction-time mistake with a clear message rather than
    surfacing it as a BadRequestError on the first ainvoke (finding #12)."""
    cfg = MCPServerConfig(name="slack", transport="stdio", command="x")
    with pytest.raises(ValueError, match="duplicate server name"):
        MCPToolset(cfg, cfg)


# ---- MCPToolset lifecycle --------------------------------------------------


@pytest.mark.asyncio
async def test_toolset_opens_then_closes_clients(monkeypatch) -> None:
    """The toolset must call __aenter__ on every client at entry and
    __aexit__ on every client at exit — otherwise stdio subprocesses leak."""
    opened: list[str] = []
    closed: list[str] = []

    class _TrackingClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self.config = config

        async def __aenter__(self) -> "_TrackingClient":
            opened.append(self.config.name)
            return self

        async def __aexit__(self, *exc_info: Any) -> None:
            closed.append(self.config.name)

        async def list_tools(self) -> list[Tool]:
            return [_tool("ping")]

    monkeypatch.setattr("hexgate.mcp.proxy.MCPClient", _TrackingClient)

    a = MCPServerConfig(name="a", transport="stdio", command="x")
    b = MCPServerConfig(name="b", transport="stdio", command="y")

    async with MCPToolset(a, b) as mcp:
        assert opened == ["a", "b"]
        assert closed == []  # nothing closed yet
        assert [t.name for t in mcp.tools] == ["mcp-a-ping", "mcp-b-ping"]

    # Exit closes in reverse order — symmetric teardown via AsyncExitStack.
    assert closed == ["b", "a"]


@pytest.mark.asyncio
async def test_toolset_cleans_up_on_partial_open_failure(monkeypatch) -> None:
    """If the second server fails to connect, the first must still be
    closed — otherwise a single bad MCP server leaks the others' transports."""
    opened: list[str] = []
    closed: list[str] = []

    class _MaybeFailingClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self.config = config

        async def __aenter__(self) -> "_MaybeFailingClient":
            if self.config.name == "b":
                raise RuntimeError("simulated connect failure on b")
            opened.append(self.config.name)
            return self

        async def __aexit__(self, *exc_info: Any) -> None:
            closed.append(self.config.name)

        async def list_tools(self) -> list[Tool]:
            return []

    monkeypatch.setattr("hexgate.mcp.proxy.MCPClient", _MaybeFailingClient)

    a = MCPServerConfig(name="a", transport="stdio", command="x")
    b = MCPServerConfig(name="b", transport="stdio", command="y")

    with pytest.raises(RuntimeError, match="simulated connect failure"):
        async with MCPToolset(a, b):
            pass  # pragma: no cover — entry should have raised

    # The first client was opened and must have been closed during teardown.
    assert opened == ["a"]
    assert closed == ["a"]


@pytest.mark.asyncio
async def test_toolset_flips_state_to_closed_on_exit(monkeypatch) -> None:
    """After exiting the with block, proxies built from this toolset must
    see ``state.open == False`` so they return a clear error envelope
    rather than calling into a torn-down client."""
    captured_states: list[Any] = []

    class _RecordingClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self.config = config

        async def __aenter__(self) -> "_RecordingClient":
            return self

        async def __aexit__(self, *exc_info: Any) -> None:
            pass

        async def list_tools(self) -> list[Tool]:
            return [_tool("ping")]

    monkeypatch.setattr("hexgate.mcp.proxy.MCPClient", _RecordingClient)

    cfg = MCPServerConfig(name="a", transport="stdio", command="x")
    toolset = MCPToolset(cfg)
    async with toolset as mcp:
        captured_states = list(mcp._states)  # noqa: SLF001 — invariant under test
        assert all(s.open for s in captured_states)

    assert all(not s.open for s in captured_states)
