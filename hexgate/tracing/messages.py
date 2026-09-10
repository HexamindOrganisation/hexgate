"""LLM prompt/completion content as audit events (scope ``hexgate.messages``).

The fourth event stream next to decisions (``hexgate.audit``), token usage
(``hexgate.usage``) and bans (``hexgate.bans``). One :class:`LlmMessageEvent`
per model call, carrying only the input messages *new to that call* plus its
completion — see ``hexgate.tracing.semconv`` for the wire contract and the
reasoning.

Mirrors ``tracing/usage.py``: the same sender registry, so one api_key means
one exporter for every event type, and the same ``HEXGATE_LOCAL_MODE`` gate.
The one addition is ``HEXGATE_LOG_MESSAGES``: message capture is on by
default — a log a customer has to discover and switch on is a log that is not
there when an incident needs it — and ``HEXGATE_LOG_MESSAGES=0`` turns it off
without touching the other streams.

Which messages count as "new" is the adapter's job; this module only lays an
already-derived event out on the wire — redacted, capped and serialised.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar
from uuid import UUID, uuid4

from hexgate.audit import (
    MAX_INPUT_MESSAGES_BYTES,
    MAX_OUTPUT_MESSAGES_BYTES,
    MAX_SYSTEM_INSTRUCTIONS_BYTES,
    SENSITIVE_ARG_KEY_RE,
    TOOL_CALL_JSON_KEYS,
    cap_json_head_tail,
    redact,
)
from hexgate.runtime.context import get_current_context
from hexgate.runtime.run_facts import get_run_facts
from hexgate.tracing import semconv
from hexgate.tracing._senders import AuditSender, get_or_create_sender
from hexgate.tracing._senders import get_sender as _get_sender
from hexgate.tracing._senders import shutdown as _shutdown_all

_log = logging.getLogger(__name__)

# Opt-out switch for this stream only. Unset means on. Read on every emit, not
# cached at import: a test or an operator flipping it mid-process must see the
# change, and the read costs nothing next to serialising a prompt.
LOG_MESSAGES_ENV = "HEXGATE_LOG_MESSAGES"

# The values that switch capture off: the falsy counterparts of the truthy
# spellings ``HEXGATE_LOCAL_MODE`` accepts, so ``=0`` and its usual synonyms all
# work. Anything else — ``1``, ``true``, an empty string, a typo — leaves the
# default (on) in force, so a mistyped value never silently disables the log.
_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def _log_messages_enabled() -> bool:
    """True unless ``HEXGATE_LOG_MESSAGES`` is set to a falsy value."""
    return os.environ.get(LOG_MESSAGES_ENV, "").strip().lower() not in _OFF_VALUES


def _json_default(obj: Any) -> Any:
    """``json.dumps`` hook for what the framework hands an adapter: pydantic
    models (every supported framework's message type), dataclasses and mappings
    become containers, other iterables become lists, and only a true scalar
    falls back to ``str()``. Each return value is fed back through ``dumps``,
    so nested models unwrap all the way down."""
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, Mapping):
        return dict(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    return str(obj)


def _as_json_data(value: Any) -> Any:
    """Round-trip ``value`` through JSON so what redaction and the caps walk is
    plain dicts, lists and scalars.

    ``redact`` and ``cap_json_head_tail`` only descend into dicts and lists; a
    message object they cannot open would be serialised whole by
    ``json.dumps(default=str)`` — a ``repr()`` in the transcript, with any
    secret key inside it unredacted. Normalising first closes that gap for
    every shape the adapters can pass, at the cost of one extra serialisation
    of a few KB per call."""
    return json.loads(json.dumps(value, default=_json_default))


def _capped_json(value: Any, *, cap: int) -> tuple[str, bool]:
    """Normalise to JSON data, redact, cap head+tail to ``cap`` serialized
    bytes, and serialise.

    Redaction uses the ``arguments`` pattern (substring match on key names,
    recursing into every dict): the tool-call messages inside a prompt carry
    the same tool arguments the decision event redacts, and blanking them in
    one place but not the other would leave the secret readable in the
    transcript. A tool-call part may carry ``arguments`` as a JSON *string*
    (the raw OpenAI wire shape), so those are parsed, redacted inside and
    serialised back — the same ``TOOL_CALL_JSON_KEYS`` the enricher passes, so
    both ends blank the same keys. Message-shape keys (``role``, ``parts``,
    ``type``, ``content``) never match it, but tool *results* inside the
    messages get the same rule,
    so a ``next_page_token`` in a tool response is blanked too — the accepted
    over-match ``hexgate.audit`` already documents for ``arguments``. Capping
    measures the same ``json.dumps(default=str)`` it then emits, so what was
    fitted to the cap is exactly what travels.
    """
    capped, truncated = cap_json_head_tail(
        redact(
            _as_json_data(value),
            pattern=SENSITIVE_ARG_KEY_RE,
            json_string_keys=TOOL_CALL_JSON_KEYS,
        ),
        cap=cap,
    )
    return json.dumps(capped, default=str), truncated


@dataclass(frozen=True, slots=True)
class LlmMessageEvent:
    """One LLM call's prompt delta and completion, emitted as a span under
    scope ``hexgate.messages``. ``occurred_at`` becomes the span's start time.

    ``input_messages`` are the messages new to this call (tool results
    included), ``output_messages`` its completion, both in the OTel GenAI
    message shape as plain dicts. ``system_instructions`` rides along on the
    first event of each ``turn_key`` only, ``None`` otherwise. ``turn_key``
    names the framework message list this event extends and ``message_seq``
    counts events within it; ``resynced`` says this event restates the whole
    list because the framework rewrote it. The caller supplies all of those —
    this class never decides what is new."""

    SCOPE: ClassVar[str] = semconv.SCOPE_MESSAGES

    agent_name: str
    model: str
    input_messages: list[Any]
    output_messages: list[Any]
    turn_key: str
    message_seq: int
    system_instructions: list[Any] | None = None
    resynced: bool = False
    session_id: str = ""
    user_id: str = ""
    # Joins this row to the policy_decision and llm_invocation rows of the same
    # run. ``""`` outside a run scope. A plain field, as on ``LlmUsageEvent``:
    # llm_message carries one run column.
    run_id: str = ""
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def span_attributes(self) -> dict[str, Any]:
        """Flat span attributes: the official ``gen_ai.*`` names for model and
        content, ``sec_ai.*`` for the envelope and the transcript bookkeeping.

        The three content fields travel as JSON strings, each redacted and
        capped to its own budget here — the single choke point onto the wire,
        as ``AuditEvent.span_attributes()`` is for ``arguments``. ``truncated``
        is the OR across them: a reader needs one flag to know the row is
        lossy, and the head+tail marker inside the field says where. Absent
        ``system_instructions`` are left out rather than sent as ``None`` or
        ``""`` — OTel attributes cannot carry null, and the platform column
        defaults to empty on its own."""
        input_json, input_cut = _capped_json(
            self.input_messages, cap=MAX_INPUT_MESSAGES_BYTES
        )
        output_json, output_cut = _capped_json(
            self.output_messages, cap=MAX_OUTPUT_MESSAGES_BYTES
        )
        attrs: dict[str, Any] = {
            semconv.EVENT_ID: str(self.event_id),
            semconv.AGENT_NAME: self.agent_name,
            semconv.SESSION_ID: self.session_id,
            semconv.USER_ID: self.user_id,
            semconv.GEN_AI_REQUEST_MODEL: self.model,
            semconv.GEN_AI_INPUT_MESSAGES: input_json,
            semconv.GEN_AI_OUTPUT_MESSAGES: output_json,
            semconv.TURN_KEY: self.turn_key,
            semconv.MESSAGE_SEQ: self.message_seq,
            semconv.RESYNCED: self.resynced,
        }
        truncated = input_cut or output_cut
        if self.system_instructions is not None:
            system_json, system_cut = _capped_json(
                self.system_instructions, cap=MAX_SYSTEM_INSTRUCTIONS_BYTES
            )
            attrs[semconv.GEN_AI_SYSTEM_INSTRUCTIONS] = system_json
            truncated = truncated or system_cut
        attrs[semconv.TRUNCATED] = truncated
        # Absent, never "": the platform rejects an empty string, losing the
        # whole message record rather than just its attribution.
        if self.run_id:
            attrs[semconv.RUN_ID] = self.run_id
        return attrs


def emit_llm_messages(
    agent_name: str,
    model: str,
    input_messages: list[Any],
    output_messages: list[Any],
    *,
    turn_key: str,
    message_seq: int,
    system_instructions: list[Any] | None = None,
    resynced: bool = False,
    api_key: str | None = None,
) -> None:
    """Resolve identity from the active HexgateContext scope and emit one
    :class:`LlmMessageEvent` through the shared sender registry.

    The single entry point every adapter's message hook calls into — never
    raises, for the same reason ``emit_llm_usage`` never does: the framework
    hooks it runs from either re-raise or don't guard the call, and a logging
    failure must not fail the agent run it is logging. A no-op when
    ``HEXGATE_LOG_MESSAGES`` is set to ``0`` (checked first, so an opted-out
    process never builds a sender for this stream alone), when no api_key is
    resolvable, or when ``HEXGATE_LOCAL_MODE`` is on — the last two exactly as
    for decisions and usage, since the registry is shared.
    """
    try:
        if not _log_messages_enabled():
            return
        sender = configure_messages_sender(api_key)
        if sender is None:
            return
        context = get_current_context()
        sender.emit(
            LlmMessageEvent(
                run_id=get_run_facts().id,
                agent_name=agent_name,
                model=model,
                input_messages=input_messages,
                output_messages=output_messages,
                turn_key=turn_key,
                message_seq=message_seq,
                system_instructions=system_instructions,
                resynced=resynced,
                session_id=context.session_id
                if (context is not None and context.session_id)
                else "",
                user_id=context.user_id if context is not None else "",
            )
        )
    except Exception:
        _log.exception("emit_llm_messages raised; ignoring")


def configure_messages_sender(
    api_key: str | None = None,
    base_url: str | None = None,
) -> AuditSender | None:
    """Get-or-create the LLM-messages sender for ``api_key``. Idempotent per
    key.

    Shares the registry (and the ``HEXGATE_LOCAL_MODE`` gate) with
    :func:`hexgate.audit.configure` and
    :func:`hexgate.tracing.usage.configure_usage_sender` via
    ``hexgate.tracing._senders`` — the same api_key returns the very same
    sender here; the span's instrumentation scope keeps the event types apart.
    """
    return get_or_create_sender(api_key, base_url)


def get_messages_sender(api_key: str | None = None) -> AuditSender | None:
    """Return the LLM-messages sender for ``api_key`` (or ``HEXGATE_API_KEY``),
    if configured. Never creates one."""
    return _get_sender(api_key)


async def shutdown() -> None:
    """Flush queued events and stop every sender in the shared registry —
    decisions, LLM usage, bans and LLM messages alike. Safe to call multiple
    times; equivalent to :func:`hexgate.audit.shutdown` — either name flushes
    the whole shared registry."""
    await _shutdown_all()
