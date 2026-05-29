"""AuditSender behavior. Mocks the httpx.AsyncClient on the sender instance."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx

from fortify.audit import AuditEvent, AuditSender
from fortify.security.decision import Decision, DecisionOutcome


def _event() -> AuditEvent:
    d = Decision(outcome=DecisionOutcome.DENY, agent_name="r", tool_name="t")
    return AuditEvent(decision=d, user_id="u", session_id="s")


def _stub_client(status: int = 202) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=status, text=""))
    client.aclose = AsyncMock()
    return client


async def test_emit_schedules_task_and_returns_immediately() -> None:
    sender = AuditSender("http://x/y", "k")
    sender._client = _stub_client()
    sender.emit(_event())
    assert len(sender._tasks) == 1
    await asyncio.gather(*sender._tasks)
    sender._client.post.assert_called_once()


async def test_emit_post_carries_endpoint_and_wire_body() -> None:
    sender = AuditSender("http://x/y", "k")
    sender._client = _stub_client()
    sender.emit(_event())
    await asyncio.gather(*sender._tasks)
    args, kwargs = sender._client.post.call_args
    assert args[0] == "http://x/y"
    assert kwargs["json"]["outcome"] == "deny"
    assert kwargs["json"]["user_id"] == "u"
    assert kwargs["json"]["session_id"] == "s"


def test_constructor_sets_bearer_header() -> None:
    """Real httpx.AsyncClient constructed in __init__ carries the bearer header."""
    sender = AuditSender("http://x/y", "k")
    assert sender._client.headers["Authorization"] == "Bearer k"


async def test_semaphore_saturation_drops_events(
    caplog: "logging.LogCaptureFixture",
) -> None:
    sender = AuditSender("http://x/y", "k", max_in_flight=1)
    await sender._semaphore.acquire()
    try:
        with caplog.at_level(logging.WARNING, logger="fortify.audit"):
            for _ in range(5):
                sender.emit(_event())
        assert sender._dropped == 5
        assert any("dropped" in r.message for r in caplog.records)
    finally:
        sender._semaphore.release()


async def test_503_triggers_one_retry() -> None:
    sender = AuditSender("http://x/y", "k", http_timeout=0.01)
    sender._client = MagicMock()
    sender._client.post = AsyncMock(
        side_effect=[
            MagicMock(status_code=503, text="busy"),
            MagicMock(status_code=202, text=""),
        ]
    )
    sender._client.aclose = AsyncMock()
    sender.emit(_event())
    await asyncio.gather(*sender._tasks)
    assert sender._client.post.await_count == 2


async def test_close_drains_in_flight_then_acloses_client() -> None:
    sender = AuditSender("http://x/y", "k")
    sender._client = _stub_client()
    sender.emit(_event())
    sender.emit(_event())
    assert len(sender._tasks) == 2
    await sender.close()
    assert len(sender._tasks) == 0
    sender._client.aclose.assert_awaited_once()


async def test_post_close_emit_is_noop() -> None:
    sender = AuditSender("http://x/y", "k")
    sender._client = _stub_client()
    await sender.close()
    sender.emit(_event())
    assert len(sender._tasks) == 0


async def test_network_error_logged_not_raised(
    caplog: "logging.LogCaptureFixture",
) -> None:
    sender = AuditSender("http://x/y", "k")
    sender._client = MagicMock()
    sender._client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    sender._client.aclose = AsyncMock()
    with caplog.at_level(logging.WARNING, logger="fortify.audit"):
        sender.emit(_event())
        await asyncio.gather(*sender._tasks)
    assert any("network error" in r.message for r in caplog.records)


def test_no_running_loop_skips_silently(caplog: "logging.LogCaptureFixture") -> None:
    """Sync caller with no event loop: emit no-ops with a one-time warning."""
    sender = AuditSender("http://x/y", "k")
    with caplog.at_level(logging.WARNING, logger="fortify.audit"):
        sender.emit(_event())
        sender.emit(_event())  # second call: silent
    assert len(sender._tasks) == 0
    assert sender._warned_no_loop is True
    no_loop_warnings = [
        r for r in caplog.records if "without a running event loop" in r.message
    ]
    assert len(no_loop_warnings) == 1
