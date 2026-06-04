"""Per-decision audit emission to the platform's /v1/audit/decisions endpoint.

Fire-and-forget POST per decision; bounded concurrency; drops on saturation.
Lifecycle: configure() per api_key, await shutdown() at process exit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from fortify.security.decision import Decision

_log = logging.getLogger(__name__)

# Mirrors the platform's MAX_ARGS_BYTES (platform/api/audit.py). The platform
# rejects (413) rather than truncates, so an over-cap event would be lost
# entirely unless the SDK trims it first.
MAX_ARGS_BYTES = 8 * 1024

# Keys whose values are stripped from the audit copy of ``arguments`` before
# transmission. A seatbelt, not a guarantee: values that are sensitive by
# content rather than key name (SQL strings, email bodies) are NOT caught.
_SENSITIVE_KEY_RE = re.compile(
    r"password|passwd|secret|token|api[-_]?key|credential|authorization",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"


def _redact(value: Any) -> Any:
    """Return a copy of ``value`` with sensitive-keyed values replaced.

    Pure — never mutates the input, so the ``Decision`` the caller holds
    keeps its full arguments; only the wire payload is redacted."""
    if isinstance(value, dict):
        return {
            k: _REDACTED if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k)
            else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _truncate_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Trim ``arguments`` to fit the platform's byte cap.

    Serialization mirrors the platform's measurement (``default=str``). Over
    the cap, the dict is replaced by a marker wrapping a JSON-text preview,
    shrunk until the wrapper itself fits — lossy, but stored; the platform
    would 413-reject the raw payload and lose the event entirely."""
    args_json = json.dumps(arguments, default=str)
    if len(args_json.encode("utf-8")) <= MAX_ARGS_BYTES:
        return arguments
    preview_bytes = MAX_ARGS_BYTES - 512
    while True:
        wrapper = {
            "_truncated": True,
            "original_bytes": len(args_json.encode("utf-8")),
            "preview": args_json.encode("utf-8")[:preview_bytes].decode(
                "utf-8", errors="ignore"
            ),
        }
        if len(json.dumps(wrapper).encode("utf-8")) <= MAX_ARGS_BYTES:
            return wrapper
        preview_bytes //= 2


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Decision plus caller identity from the active User scope."""

    decision: Decision
    user_id: str = ""
    session_id: str = ""

    def as_payload(self) -> dict[str, Any]:
        """Flat JSON payload matching the platform's DecisionEvent body.

        ``arguments`` are redacted (sensitive key names) and truncated to the
        platform byte cap here — the single choke point onto the wire."""
        d = self.decision
        arguments = (
            _truncate_args(_redact(d.arguments))
            if d.arguments is not None
            else None
        )
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
            "arguments":   arguments,
            "user_id":     self.user_id,
            "session_id":  self.session_id,
        }


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
        self._api_key = api_key
        self._max_in_flight = max_in_flight
        self._http_timeout = http_timeout
        # The semaphore and httpx client are loop-bound: asyncio primitives
        # latch onto the first loop that drives them and reject any other
        # (e.g. a second asyncio.run()). Build them eagerly so configure()
        # stays sync, but track the loop and rebuild if it rotates.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._client: httpx.AsyncClient | None = self._new_client()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._dropped = 0
        self._warned_no_loop = False

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._http_timeout,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    def _ensure_loop_state(self, loop: asyncio.AbstractEventLoop) -> None:
        """Adopt the running loop on first use; rebuild on loop rotation.

        The previous client/semaphore are bound to a now-defunct loop, so
        drop them (GC closes the old client) and rebuild on ``loop``."""
        if self._loop is loop:
            return
        if self._loop is not None:
            self._semaphore = asyncio.Semaphore(self._max_in_flight)
            self._client = self._new_client()
            self._dropped = 0
        self._loop = loop

    def emit(self, event: AuditEvent) -> None:
        if self._closing or self._client is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if not self._warned_no_loop:
                _log.warning("audit emit called without a running event loop; skipping")
                self._warned_no_loop = True
            return
        self._ensure_loop_state(loop)
        if self._semaphore.locked():
            self._dropped += 1
            if self._dropped % 100 == 1:
                _log.warning(
                    "audit sender saturated; %d events dropped (platform slow?)",
                    self._dropped,
                )
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


# --- Per-key sender registry --------------------------------------------------


_AUDIT_PATH = "/v1/audit/decisions"
_DEFAULT_API_URL = "http://localhost:8000"
# One sender per resolved api_key. A single process may wrap agents for
# several tenants/keys, and each must emit with its own bearer token — so
# senders are keyed by api_key rather than kept as a first-wins singleton.
_senders: dict[str, AuditSender] = {}


def configure(
    api_key: str | None = None,
    base_url: str | None = None,
) -> AuditSender | None:
    """Get-or-create the audit sender for ``api_key``. Idempotent per key.

    Both args fall back to ``FORTIFY_KEY`` / ``FORTIFY_API_URL`` env vars.
    Reuses the existing sender when the same key was already configured;
    distinct keys get distinct senders. Returns ``None`` when no api_key is
    resolvable — audit stays inert.
    """
    resolved_key = api_key or os.environ.get("FORTIFY_KEY")
    if not resolved_key:
        return None
    existing = _senders.get(resolved_key)
    if existing is not None:
        return existing
    resolved_url = base_url or os.environ.get("FORTIFY_API_URL", _DEFAULT_API_URL)
    sender = AuditSender(
        endpoint=f"{resolved_url.rstrip('/')}{_AUDIT_PATH}",
        api_key=resolved_key,
    )
    _senders[resolved_key] = sender
    return sender


def get_sender(api_key: str | None = None) -> AuditSender | None:
    """Return the audit sender for ``api_key`` (or ``FORTIFY_KEY``), if configured.

    Production code should use the sender injected into
    :class:`~fortify.security.enforcer.PolicyEnforcer`; this lookup exists for
    diagnostics and is unambiguous only when scoped to a key.
    """
    resolved_key = api_key or os.environ.get("FORTIFY_KEY")
    if not resolved_key:
        return None
    return _senders.get(resolved_key)


async def shutdown() -> None:
    """Drain in-flight emits and close every sender. Safe to call multiple times."""
    senders = list(_senders.values())
    _senders.clear()
    await asyncio.gather(*(s.close() for s in senders), return_exceptions=True)
