"""Tests for the serve-mode → User scope handoff.

After Phase 3.5, serve.py owns only the WebSocket plumbing: it parses
``user_attenuation`` metadata into a :class:`fortify.runtime.User`, wraps
the agent invocation in ``async with User(...)``, and lets the runtime
attenuate lazily. These tests cover the parsing helper and the
end-to-end handler shape (with stream_agent monkeypatched out).
"""

from __future__ import annotations

from typing import Any

import pytest
from rich.console import Console

from fortify.cli import serve
from fortify.cli.serve import ServeContext, _user_from_payload
from fortify.cli.state import ChatState
from fortify.runtime import User, get_current_user


# ---------------------------------------------------------------------------
# _user_from_payload — happy / malformed
# ---------------------------------------------------------------------------


def test_user_from_payload_returns_user_with_all_fields() -> None:
    """A complete payload yields a fully-populated User."""
    user = _user_from_payload(
        {
            "user": "alice",
            "role": "billing",
            "session_id": "sess_abc",
            "ttl_seconds": 300,
        }
    )
    assert user is not None
    assert user.user_id == "alice"
    assert user.role == "billing"
    assert user.session_id == "sess_abc"
    assert user.ttl_seconds == 300


def test_user_from_payload_returns_user_with_just_user_id() -> None:
    """Minimal ``{"user": ...}`` is enough."""
    user = _user_from_payload({"user": "bob"})
    assert user is not None
    assert user.user_id == "bob"
    assert user.role is None


def test_user_from_payload_returns_none_for_empty_dict() -> None:
    """An empty dict means no user requested → no scope."""
    assert _user_from_payload({}) is None


def test_user_from_payload_returns_none_for_missing_user_key() -> None:
    """Without a ``user`` key the payload doesn't drive a scope."""
    assert _user_from_payload({"scope": ["read"]}) is None


def test_user_from_payload_returns_none_for_non_dict() -> None:
    """Lists / strings / Nones all yield no User."""
    assert _user_from_payload(None) is None
    assert _user_from_payload("alice") is None
    assert _user_from_payload(["alice"]) is None


def test_user_from_payload_returns_none_on_invalid_shape() -> None:
    """A payload with the wrong type for ttl trips Pydantic validation."""
    assert _user_from_payload({"user": "alice", "ttl_seconds": "not-a-number"}) is None


# ---------------------------------------------------------------------------
# _handle_message wraps the agent invocation in the User scope
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal ws stub recording every outbound frame."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, frame: str) -> None:
        self.sent.append(frame)


class _FakeRuntime:
    """Runtime stand-in — what `_handle_message` reads off ``context.runtime``."""

    def __init__(self) -> None:
        self.agent_name = "fake-agent"
        self.agent = object()
        self.handler = object()


@pytest.mark.asyncio
async def test_handle_message_chat_with_attenuation_enters_user_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat payload with ``user_attenuation`` enters a User scope around stream_agent."""
    captured: dict[str, Any] = {"user_during_stream": None}

    async def fake_stream_agent(
        agent: object, handler: object, input: object, **kw: Any
    ):
        captured["user_during_stream"] = get_current_user()
        if False:
            yield None  # pragma: no cover

    monkeypatch.setattr(serve, "stream_agent", fake_stream_agent)

    # ``api_key`` is required on ServeContext post-Phase-6 (used to
    # build the WS bearer subprotocol). _handle_message doesn't touch
    # it, so a placeholder is fine for these unit-level tests.
    context = ServeContext(runtime=_FakeRuntime(), state=ChatState(), api_key="")
    ws = _FakeWebSocket()

    await serve._handle_message(
        context,
        ws,
        {
            "type": "chat",
            "message": "refund 30",
            "user_attenuation": {
                "user": "alice",
                "role": "billing",
            },
        },
    )

    captured_user: User | None = captured["user_during_stream"]
    assert captured_user is not None
    assert captured_user.user_id == "alice"
    assert captured_user.role == "billing"

    # After the handler returns the scope must be cleanly popped.
    assert get_current_user() is None


@pytest.mark.asyncio
async def test_handle_message_chat_without_attenuation_runs_with_no_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat: messages without ``user_attenuation`` see no User scope."""
    captured: dict[str, Any] = {"user_during_stream": "sentinel"}

    async def fake_stream_agent(
        agent: object, handler: object, input: object, **kw: Any
    ):
        captured["user_during_stream"] = get_current_user()
        if False:
            yield None  # pragma: no cover

    monkeypatch.setattr(serve, "stream_agent", fake_stream_agent)

    # ``api_key`` is required on ServeContext post-Phase-6 (used to
    # build the WS bearer subprotocol). _handle_message doesn't touch
    # it, so a placeholder is fine for these unit-level tests.
    context = ServeContext(runtime=_FakeRuntime(), state=ChatState(), api_key="")
    ws = _FakeWebSocket()

    await serve._handle_message(context, ws, {"type": "chat", "message": "hello"})

    assert captured["user_during_stream"] is None


@pytest.mark.asyncio
async def test_handle_message_malformed_attenuation_runs_without_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed user_attenuation payload still lets the turn run (no scope)."""
    captured: dict[str, Any] = {"user_during_stream": "sentinel"}

    async def fake_stream_agent(
        agent: object, handler: object, input: object, **kw: Any
    ):
        captured["user_during_stream"] = get_current_user()
        if False:
            yield None  # pragma: no cover

    monkeypatch.setattr(serve, "stream_agent", fake_stream_agent)

    # ``api_key`` is required on ServeContext post-Phase-6 (used to
    # build the WS bearer subprotocol). _handle_message doesn't touch
    # it, so a placeholder is fine for these unit-level tests.
    context = ServeContext(runtime=_FakeRuntime(), state=ChatState(), api_key="")
    ws = _FakeWebSocket()

    await serve._handle_message(
        context,
        ws,
        {
            "type": "chat",
            "message": "hello",
            "user_attenuation": "not-a-dict",  # ignored, no scope
        },
    )

    assert captured["user_during_stream"] is None


# ---------------------------------------------------------------------------
# Phase 6 — bearer-subprotocol WS handshake
# ---------------------------------------------------------------------------


class _FakeWsForLoop:
    """``async with`` stand-in matching what ``connect()`` returns."""

    def __init__(self, subprotocol: str | None = "fortify.v1") -> None:
        self.subprotocol = subprotocol
        self.sent: list[str] = []

    async def __aenter__(self) -> "_FakeWsForLoop":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def send(self, frame: str) -> None:
        self.sent.append(frame)

    def __aiter__(self):
        async def _empty():
            if False:
                yield None  # pragma: no cover
        return _empty()


@pytest.mark.asyncio
async def test_serve_loop_offers_bearer_and_marker_subprotocols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_serve_loop`` connects with ``bearer.<key>`` + ``fortify.v1``.

    Pins the Phase 6 WS auth contract: the CLI offers the bearer in
    ``Sec-WebSocket-Protocol`` (the only way to authenticate a WS
    handshake from a browser; we match the contract from native
    Python for consistency). Without the bearer subprotocol the
    server closes with 4401 before accept.

    Real biscuit tokens end with ``=`` padding, which the RFC 7230
    token grammar (inherited by WebSocket subprotocols) forbids. The
    CLI percent-encodes the envelope before placing it in the
    subprotocol value — exercised here with a key containing ``=``.
    """
    captured: dict[str, Any] = {}

    def fake_connect(url: str, **kwargs: Any) -> _FakeWsForLoop:
        captured["url"] = url
        captured["subprotocols"] = kwargs.get("subprotocols")
        captured["ping_interval"] = kwargs.get("ping_interval")
        return _FakeWsForLoop(subprotocol="fortify.v1")

    monkeypatch.setattr(serve, "connect", fake_connect)

    # Realistic shape: includes the ``=`` padding biscuit emits.
    api_key = "fty_live_acme_AbCdEf123-_=="
    context = ServeContext(
        runtime=_FakeRuntime(),
        state=ChatState(),
        api_key=api_key,
    )
    await serve._serve_loop(context, "ws://test/v1/serve", Console())

    assert captured["url"] == "ws://test/v1/serve"
    # The bearer subprotocol carries the percent-encoded envelope:
    # ``=`` → ``%3D`` (the only non-token char in URL-safe base64).
    assert captured["subprotocols"] == [
        "bearer.fty_live_acme_AbCdEf123-_%3D%3D",
        "fortify.v1",
    ]
    # No ``=`` survives into the wire format — sanity check for
    # anyone inspecting the subprotocol grammar.
    assert "=" not in captured["subprotocols"][0]
    assert captured["ping_interval"] == serve.PING_INTERVAL


@pytest.mark.asyncio
async def test_serve_loop_aborts_when_marker_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server didn't negotiate ``fortify.v1`` → FortifyError before any send.

    Defense against accidentally talking to a pre-Phase-6 server that
    silently ignores the unknown ``bearer.`` subprotocol and accepts
    the handshake without honoring the auth contract. Without this
    check the CLI would happily relay chats with no auth at all.
    """
    def fake_connect(url: str, **kwargs: Any) -> _FakeWsForLoop:
        return _FakeWsForLoop(subprotocol=None)  # no marker echoed

    monkeypatch.setattr(serve, "connect", fake_connect)

    context = ServeContext(
        runtime=_FakeRuntime(),
        state=ChatState(),
        api_key="fty_live_acme_secret",
    )

    with pytest.raises(serve.FortifyError, match="fortify.v1"):
        await serve._serve_loop(context, "ws://test/v1/serve", Console())
