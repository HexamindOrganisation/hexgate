"""``run_start`` span, scope ``hexgate.runs``: one per admitted agent run.

Emitted by :func:`hexgate.runtime.run_facts.run_scope`, which resolves identity
itself, so this module stays free of ``hexgate.runtime`` (run_facts imports it).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar
from uuid import UUID, uuid4

from hexgate.tracing import semconv
from hexgate.tracing._senders import get_or_create_sender

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunStartEvent:
    SCOPE: ClassVar[str] = semconv.SCOPE_RUNS

    agent_name: str
    run_id: str
    session_id: str = ""
    user_id: str = ""
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def span_attributes(self) -> dict[str, Any]:
        return {
            semconv.EVENT_ID: str(self.event_id),
            semconv.AGENT_NAME: self.agent_name,
            semconv.SESSION_ID: self.session_id,
            semconv.USER_ID: self.user_id,
            # Always present: unlike the other scopes, this span only exists
            # inside a run, and the platform rejects it without one.
            semconv.RUN_ID: self.run_id,
        }


def emit_run_start(
    agent_name: str,
    run_id: str,
    *,
    session_id: str = "",
    user_id: str = "",
    api_key: str | None = None,
) -> None:
    """Emit one :class:`RunStartEvent` if a sender is configured for ``api_key``.

    Never raises: it runs on every run's entry path, and a telemetry failure must
    not fail the run. A no-op with no key or under ``HEXGATE_LOCAL_MODE``.
    """
    try:
        sender = get_or_create_sender(api_key)
        if sender is None:
            return
        sender.emit(
            RunStartEvent(
                agent_name=agent_name,
                run_id=run_id,
                session_id=session_id,
                user_id=user_id,
            )
        )
    except Exception:
        _log.exception("emit_run_start raised; ignoring")
