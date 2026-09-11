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

Which messages are "new" is decided here too, by :class:`MessageCursor`: every
adapter hook is handed the *whole* input list and has to work out what changed
since its last call. One implementation shared by the four adapters rather than
four subtly different ones.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import threading
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


# --- The delta cursor ---------------------------------------------------------

# Fingerprint of the empty prefix — the state a turn_key starts in, so a first
# call takes the ordinary slice path with count=0 rather than a special case.
_EMPTY_FINGERPRINT = "0:"

# A fingerprint no list can produce — every real one carries a count and a
# digest. Stored when fingerprinting failed, so the next call resyncs instead
# of slicing against a mark we never established.
_UNMATCHABLE_FINGERPRINT = "?"


def _canonical(message: Any) -> bytes:
    """One message as stable bytes.

    ``sort_keys`` so a dict rebuilt in another key order is still recognised as
    the same message. ``default=str`` covers a framework object an adapter did
    not flatten — which makes the fingerprint only as stable as that object's
    ``__str__``: a type falling back to ``object.__repr__`` stringifies to its
    memory address and never matches itself, so every call resyncs. Adapters
    hand over dicts (or pydantic models, whose repr is by value) for that
    reason.

    ``json.dumps`` raises on a few shapes ``default=`` does not reach — a dict
    key that is not a scalar, mixed key types under ``sort_keys``, a reference
    cycle — so callers must treat this as fallible. :meth:`MessageCursor.advance`
    does.
    """
    return json.dumps(message, sort_keys=True, default=str).encode("utf-8")


def _digest(first: bytes, last: bytes) -> str:
    digest = hashlib.blake2b(digest_size=16)
    digest.update(first)
    digest.update(b"\x00")
    digest.update(last)
    return digest.hexdigest()


def _fingerprint(count: int, first: bytes, last: bytes) -> str:
    """Identify a prefix by its length and its first and last message.

    Not a hash of the whole list: this runs on every LLM call, and the input
    list is the entire conversation so far — hashing all of it would make each
    call cost O(conversation). First+last catches what actually happens to
    these lists. A trim drops the front, so the first message changes; a
    summary replaces the front with one summary message, same; an append
    changes the length. What it cannot see is a rewrite strictly in the middle
    that preserves length, first and last — no framework does that today, and
    the cost of being wrong is one stale message in the transcript, not a
    corrupt slice.

    The length leads the string, so a list that shrank below the mark fails the
    comparison on the count alone and there is no separate length check to
    forget.
    """
    return _EMPTY_FINGERPRINT if count == 0 else f"{count}:{_digest(first, last)}"


@dataclass(frozen=True, slots=True)
class _TurnState:
    """What the cursor remembers about one message list: how far it has
    emitted, which messages those were, and the seq the next event gets."""

    count: int
    fingerprint: str
    next_seq: int


_FRESH_TURN = _TurnState(count=0, fingerprint=_EMPTY_FINGERPRINT, next_seq=0)


@dataclass(frozen=True, slots=True)
class MessageDelta:
    """What one LLM call adds to a transcript: the messages to emit, the
    ``message_seq`` to emit them under, and whether they restate the whole list
    instead of extending it.

    ``seq == 0`` is exactly "first event of this ``turn_key``", which is the
    adapter's cue to attach ``system_instructions`` — a resync keeps counting,
    so it never looks like a fresh turn.

    The caller must emit an event for every delta it asks for, empty
    ``messages`` included. The seq is spent by the call that produced it, so
    skipping an emit leaves a hole that a reader is specified to read as a lost
    row. An empty delta is not an anomaly anyway: the input list is unchanged
    but the completion is still new."""

    messages: list[Any]
    seq: int
    resynced: bool


class MessageCursor:
    """Per-``turn_key`` high-water marks, turning each hook's whole input list
    into just what is new.

    One cursor is shared by every run an adapter serves, so state is keyed by
    ``turn_key`` — the framework's own identity for *one message list*, not one
    session. Handoffs and sub-agents share a ``session_id`` while each keeps
    its own list; keyed by session, a sub-agent's first call would look like a
    twenty-message jump and resync forever.

    Pure bookkeeping over plain dicts: it never inspects a role, never filters,
    and never decides to emit. Tool results ride through with everything else —
    decision events record that a tool was called but never what it returned,
    so the transcript is the only place that value is stored.

    State is kept until :meth:`reset` drops it, which every adapter owes this
    class on run end — a few hundred bytes per live message list, and nothing
    of the conversation itself. That contract is the whole memory bound on
    purpose: an LRU here would evict a list whose run is still going, and the
    cursor cannot tell that key apart from one it has never seen, so the
    comeback would go out as a *fresh turn* — the full history again, at seq 0,
    not flagged ``resynced``, and with the turn's seq counter rewound past the
    gap detection the reader relies on. An adapter that forgets ``reset`` leaks
    slowly and visibly; silently corrupting a transcript is the worse trade.
    """

    def __init__(self) -> None:
        self._turns: dict[str, _TurnState] = {}
        # Adapter hooks are not all async: LangChain runs sync handlers on
        # whatever thread the chain is on, and one cursor serves concurrent
        # runs. The critical section is two message hashes, a list slice and
        # two dict operations.
        self._lock = threading.Lock()

    def advance(self, turn_key: str, messages: list[Any]) -> MessageDelta:
        """Record ``messages`` as the current state of ``turn_key`` and return
        what is new since the last call.

        Extension is the normal case: the prefix we last emitted is still
        there, so everything past the mark is new. Otherwise the framework
        rewrote the list to fit a context window — trimmed old turns, or
        replaced them with a summary — and slicing at a mark that no longer
        means anything would emit the wrong tail, or nothing at all. Then the
        whole list goes out under ``resynced``, and the reader is told the
        history was restated rather than continued.

        Never raises. It runs from a framework hook that re-raises into the
        agent run, and fingerprinting is the one step here that touches raw,
        un-flattened framework data — the same rule ``emit_llm_messages`` and
        ``AuditSender.emit`` already keep, so no adapter has to remember it.
        A failure degrades to a resync: restating the list is what "I lost my
        place" already means on the wire, and it costs a bigger event, not a
        wrong one.
        """
        with self._lock:
            state = self._turns.get(turn_key, _FRESH_TURN)
            try:
                new, resynced, mark = self._diff(messages, state)
            except Exception:
                _log.exception("fingerprinting messages failed; resyncing %s", turn_key)
                new, resynced = list(messages), True
                mark = _TurnState(0, _UNMATCHABLE_FINGERPRINT, 0)
            self._turns[turn_key] = _TurnState(
                count=mark.count,
                fingerprint=mark.fingerprint,
                next_seq=state.next_seq + 1,
            )
            return MessageDelta(messages=new, seq=state.next_seq, resynced=resynced)

    @staticmethod
    def _diff(
        messages: list[Any], state: _TurnState
    ) -> tuple[list[Any], bool, _TurnState]:
        """Split ``messages`` against ``state`` and fingerprint it for next
        time. ``messages[0]`` is canonicalised once and used for both
        fingerprints: in steady state it is the same message every call, and
        one of these lists can carry an inlined image."""
        if not messages:
        if not messages:
            return [], state.count > 0, _TurnState(0, _UNMATCHABLE_FINGERPRINT, 0)
        first = _canonical(messages[0])
        current = _fingerprint(len(messages), first, _canonical(messages[-1]))
        mark = _TurnState(count=len(messages), fingerprint=current, next_seq=0)
        if state.count == 0:
            prefix = _EMPTY_FINGERPRINT
        elif state.count > len(messages):
            # The list shrank below the mark, so it cannot carry the prefix we
            # emitted. The count inside the fingerprint would say so anyway,
            # but indexing for it would raise first.
            prefix = _UNMATCHABLE_FINGERPRINT
        else:
            prefix = _fingerprint(
                state.count, first, _canonical(messages[state.count - 1])
            )
        if prefix == state.fingerprint:
            return messages[state.count :], False, mark
        return list(messages), True, mark

    def reset(self, turn_key: str) -> None:
        """Forget one message list, on run end. Unknown keys are fine: a run
        that never emitted still ends, and an adapter should not have to
        remember whether it did."""
        with self._lock:
            self._turns.pop(turn_key, None)

    def clear(self) -> None:
        """Forget every message list. For process teardown and tests."""
        with self._lock:
            self._turns.clear()
