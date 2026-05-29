"""End-to-end audit emission against a live platform + ClickHouse.

Requires: `make clickhouse-up` and `make platform-api` running, and
`FORTIFY_KEY` set to a token minted via the dashboard.

Opt in with: `pytest -m integration`.
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest

import fortify.audit as audit_mod
from fortify.audit import AuditEvent
from fortify.security.decision import Decision, DecisionOutcome

pytestmark = pytest.mark.integration

PLATFORM_URL = os.environ.get("FORTIFY_API_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("FORTIFY_KEY")


def _need_token() -> None:
    if not TOKEN:
        pytest.skip("FORTIFY_KEY not set; mint a token via the dashboard")


def _event() -> AuditEvent:
    d = Decision(
        outcome=DecisionOutcome.DENY,
        agent_name="integration_agent",
        tool_name="read_file",
        role="analyst",
        reason="integration test",
    )
    return AuditEvent(decision=d, user_id="u_test", session_id="s_test")


async def test_wire_format_accepted_by_platform() -> None:
    """Manual POST proves the SDK wire format matches the platform body model."""
    _need_token()
    ev = _event()
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(
            f"{PLATFORM_URL}/v1/audit/decisions",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=ev.to_wire(),
        )
    assert response.status_code == 202, f"{response.status_code}: {response.text}"
    assert response.json()["event_id"] == str(ev.decision.event_id)


async def test_sender_emits_end_to_end_without_errors() -> None:
    """Drives the full SDK path: configure → emit → drain. Confirms no raised exceptions."""
    _need_token()
    audit_mod._sink = None  # reset for a clean configure
    sink = audit_mod.configure(f"{PLATFORM_URL}/v1/audit/decisions", TOKEN)
    try:
        sink.emit(_event())
        results = await asyncio.gather(*sink._tasks, return_exceptions=True)  # type: ignore[attr-defined]
        for r in results:
            assert not isinstance(r, BaseException), f"task raised: {r}"
    finally:
        await audit_mod.shutdown()
