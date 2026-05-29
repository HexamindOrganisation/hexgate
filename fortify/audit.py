"""Per-decision audit emission to the platform's /v1/audit/decisions endpoint.

Fire-and-forget POST per decision; bounded concurrency; drops on saturation.
Lifecycle: configure() once, await shutdown() at process exit.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from fortify.security.decision import Decision

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Decision plus caller identity from the active User scope."""

    decision: Decision
    user_id: str = ""
    session_id: str = ""

    def as_payload(self) -> dict[str, Any]:
        """Flat JSON payload matching the platform's DecisionEvent body."""
        d = self.decision
        return {
            "event_id":    str(d.event_id),
            "occurred_at": d.occurred_at.isoformat(),
            "agent_name":  d.agent_name,
            "tool_name":   d.tool_name,
            "outcome":     d.outcome.value,
            "role":        d.role or "",
            "error_type":  d.error_type or "",
            "reason":      d.reason,
            "violations":  list(d.violations),
            "hint":        d.hint,
            "arguments":   d.arguments,
            "user_id":     self.user_id,
            "session_id":  self.session_id,
        }


@runtime_checkable
class AuditSink(Protocol):
    """Emission seam — swap AuditSender for AuditBatcher later without call-site changes."""

    def emit(self, event: AuditEvent) -> None: ...
    async def close(self) -> None: ...


class AuditSender:
    """Per-decision fire-and-forget POST. Bounded by an asyncio.Semaphore.

    emit() is sync and non-blocking — schedules a background task. Drops with
    a periodic log when the semaphore is saturated (platform slow/unreachable).
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        max_in_flight: int = 32,
        http_timeout: float = 5.0,
    ) -> None:
        self._endpoint = endpoint
        self._http_timeout = http_timeout
        self._semaphore = asyncio.Semaphore(max_in_flight)
        # httpx.AsyncClient is loop-agnostic at construction; it binds to a
        # loop only at first use, so eager init keeps configure() sync.
        self._client: httpx.AsyncClient | None = httpx.AsyncClient(
            timeout=http_timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._dropped = 0
        self._warned_no_loop = False

    def emit(self, event: AuditEvent) -> None:
        if self._closing or self._client is None:
            return
        if self._semaphore.locked():
            self._dropped += 1
            if self._dropped % 100 == 1:
                _log.warning(
                    "audit sender saturated; %d events dropped (platform slow?)",
                    self._dropped,
                )
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if not self._warned_no_loop:
                _log.warning("audit emit called without a running event loop; skipping")
                self._warned_no_loop = True
            return
        task = asyncio.create_task(self._send(event), name="fortify-audit-send")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, event: AuditEvent) -> None:
        assert self._client is not None
        async with self._semaphore:
            payload = event.as_payload()
            try:
                response = await self._client.post(self._endpoint, json=payload)
                if response.status_code == 503:
                    await asyncio.sleep(min(self._http_timeout, 2.0))
                    response = await self._client.post(self._endpoint, json=payload)
                if response.status_code >= 400:
                    _log.error(
                        "audit ingest failed: %s %s",
                        response.status_code, response.text[:200],
                    )
            except httpx.RequestError as exc:
                _log.warning("audit ingest network error: %s", exc)

    async def close(self, drain_timeout: float = 5.0) -> None:
        """Stop accepting new emits; drain in-flight tasks; close the HTTP client."""
        self._closing = True
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=drain_timeout,
                )
            except asyncio.TimeoutError:
                _log.warning(
                    "audit close: drain timed out with %d tasks pending",
                    len(self._tasks),
                )
        if self._client is not None:
            await self._client.aclose()


# --- Process-wide singleton ---------------------------------------------------


_AUDIT_PATH = "/v1/audit/decisions"
_DEFAULT_API_URL = "http://localhost:8000"
_sink: AuditSink | None = None


def configure(
    api_key: str | None = None,
    base_url: str | None = None,
) -> AuditSink | None:
    """Initialize the process-wide audit sink. Idempotent.

    Both args fall back to ``FORTIFY_KEY`` / ``FORTIFY_API_URL`` env vars.
    Returns ``None`` when no api_key is resolvable — audit stays inert.
    """
    global _sink
    if _sink is not None:
        return _sink
    resolved_key = api_key or os.environ.get("FORTIFY_KEY")
    if not resolved_key:
        return None
    resolved_url = base_url or os.environ.get("FORTIFY_API_URL", _DEFAULT_API_URL)
    _sink = AuditSender(
        endpoint=f"{resolved_url.rstrip('/')}{_AUDIT_PATH}",
        api_key=resolved_key,
    )
    return _sink


def get_sink() -> AuditSink | None:
    """Return the process-wide audit sink, or None if not configured."""
    return _sink


async def shutdown() -> None:
    """Drain in-flight emits and close the sink. Safe to call multiple times."""
    global _sink
    sink = _sink
    _sink = None
    if sink is not None:
        await sink.close()
