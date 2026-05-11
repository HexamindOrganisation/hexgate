"""Tests for the serve-mode attenuation handoff.

The serve loop receives chat messages over a WebSocket. When a turn includes
``user_attenuation`` metadata (the dashboard's "Act as alice" affordance),
the loop attenuates the local FORTIFY_KEY in-process and passes the new
facts to the agent via ``ToolUseContext.biscuit_facts``.

These tests exercise ``_build_attenuated_context`` directly and the full
``_handle_message`` path with a fake agent + websocket — no real platform
connection needed.
"""

from __future__ import annotations

from typing import Any

import pytest
from biscuit_auth import BiscuitBuilder, KeyPair

from fortify.cli import serve
from fortify.cli.serve import ServeContext, _build_attenuated_context
from fortify.cli.state import ChatState
from fortify.cloud.client import FortifyClient, FortifyConfig
from fortify.runtime import ToolUseContext


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def keys() -> tuple[bytes, bytes]:
    """Fresh Ed25519 keypair as raw bytes ``(priv, pub)``."""
    kp = KeyPair()
    return kp.private_key.to_bytes(), kp.public_key.to_bytes()


def _parent_envelope(priv: bytes, project: str = "support-bot") -> str:
    """Mint a fresh project token envelope to seed the serve client with."""
    from biscuit_auth import Algorithm, PrivateKey

    pk = PrivateKey.from_bytes(priv, Algorithm.Ed25519)
    biscuit = BiscuitBuilder(f'project("{project}");').build(pk)
    return f"fty_live_{project}_{biscuit.to_base64()}"


def _client(priv: bytes, pub: bytes) -> FortifyClient:
    """A FortifyClient with the parent token + pubkey wired up."""
    return FortifyClient(
        FortifyConfig(
            base_url="http://test",
            api_key=_parent_envelope(priv),
            project_id="support-bot",
            public_key=pub,
        )
    )


# ---------------------------------------------------------------------------
# _build_attenuated_context — happy path + edge cases
# ---------------------------------------------------------------------------


def test_build_attenuated_context_returns_facts_from_attenuation(
    keys: tuple[bytes, bytes],
) -> None:
    """Per-turn metadata → attenuated token → facts in the returned context."""
    priv, pub = keys
    context = ServeContext(
        runtime=None,
        state=ChatState(),
        client=_client(priv, pub),
    )

    ctx = _build_attenuated_context(
        context,
        {
            "user": "alice",
            "limits": {"refund_limit": 50},
            "scope": ["refund"],
        },
    )
    assert ctx is not None
    facts = ctx.biscuit_facts or {}
    assert facts["user"] == ["alice"]
    assert facts["refund_limit"] == [50]
    assert facts["scope"] == ["refund"]
    assert facts["project"] == ["support-bot"]


def test_build_attenuated_context_returns_none_without_client() -> None:
    """No FortifyClient configured → no attenuation; turn runs without facts."""
    context = ServeContext(runtime=None, state=ChatState(), client=None)
    assert (
        _build_attenuated_context(context, {"user": "alice"}) is None
    )


def test_build_attenuated_context_returns_none_when_attenuation_fails(
    keys: tuple[bytes, bytes],
) -> None:
    """A bad attenuation payload (e.g. invalid limit name) degrades gracefully."""
    priv, pub = keys
    context = ServeContext(
        runtime=None,
        state=ChatState(),
        client=_client(priv, pub),
    )
    # Limit name with hyphens fails the Datalog identifier rule
    assert (
        _build_attenuated_context(
            context,
            {"user": "alice", "limits": {"Refund-Limit": 50}},
        )
        is None
    )


def test_build_attenuated_context_accepts_minimal_user_only(
    keys: tuple[bytes, bytes],
) -> None:
    """A bare ``{"user": ...}`` payload still attenuates."""
    priv, pub = keys
    context = ServeContext(
        runtime=None,
        state=ChatState(),
        client=_client(priv, pub),
    )

    ctx = _build_attenuated_context(context, {"user": "bob"})
    assert ctx is not None
    facts = ctx.biscuit_facts or {}
    assert facts["user"] == ["bob"]


# ---------------------------------------------------------------------------
# _handle_message — chat with attenuation threads facts to stream_agent
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
async def test_handle_message_threads_attenuated_context_to_stream_agent(
    keys: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat with user_attenuation → stream_agent gets a ToolUseContext with facts."""
    priv, pub = keys
    captured: dict[str, Any] = {}

    async def fake_stream_agent(
        agent: object,
        handler: object,
        input: object,
        *,
        tool_use_context: ToolUseContext | None = None,
    ):
        captured["tool_use_context"] = tool_use_context
        if False:  # never yields — just records arguments
            yield None  # pragma: no cover

    monkeypatch.setattr(serve, "stream_agent", fake_stream_agent)

    context = ServeContext(
        runtime=_FakeRuntime(),
        state=ChatState(),
        client=_client(priv, pub),
    )
    ws = _FakeWebSocket()

    await serve._handle_message(
        context,
        ws,
        {
            "type": "chat",
            "message": "refund 30",
            "user_attenuation": {
                "user": "alice",
                "limits": {"refund_limit": 50},
            },
        },
    )

    ctx = captured.get("tool_use_context")
    assert isinstance(ctx, ToolUseContext)
    facts = ctx.biscuit_facts or {}
    assert facts["user"] == ["alice"]
    assert facts["refund_limit"] == [50]


@pytest.mark.asyncio
async def test_handle_message_passes_no_context_without_attenuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat: messages without ``user_attenuation`` keep the old shape."""
    captured: dict[str, Any] = {"called": False}

    async def fake_stream_agent(
        agent: object,
        handler: object,
        input: object,
        **kwargs: Any,
    ):
        captured["called"] = True
        captured["kwargs"] = kwargs
        if False:
            yield None  # pragma: no cover

    monkeypatch.setattr(serve, "stream_agent", fake_stream_agent)

    context = ServeContext(
        runtime=_FakeRuntime(),
        state=ChatState(),
        client=None,  # no client at all — local-only mode
    )
    ws = _FakeWebSocket()

    await serve._handle_message(
        context, ws, {"type": "chat", "message": "hello"}
    )

    assert captured["called"]
    assert "tool_use_context" not in captured["kwargs"]
