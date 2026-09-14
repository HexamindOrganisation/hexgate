"""Per-decision audit emission: one OTel span per decision, exported over
OTLP to the Hexgate Collector under instrumentation scope ``hexgate.audit``.

Fire-and-forget; batched in memory; drops on saturation.
Lifecycle: configure() per api_key, await shutdown() at process exit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID, uuid4

from hexgate.tracing import semconv
from hexgate.tracing._senders import AuditSender, get_or_create_sender
from hexgate.tracing._senders import get_sender as _get_sender
from hexgate.tracing._senders import shutdown as _shutdown_all

if TYPE_CHECKING:
    # Annotation-only: Decision is used solely as a type hint below, so it stays
    # out of the runtime import graph (PEP 563 keeps it lazy). audit.py is a
    # low-level module — importing it should not drag in the whole security
    # package. The audit → security → enforcer → audit cycle is independently
    # avoided (binding.py keeps its enforcer import under TYPE_CHECKING too), so
    # this is correctness-by-design, not a workaround — keep it here.
    from hexgate.security.decision import Decision

# Mirrors the platform's MAX_ARGS_BYTES (platform/api/audit.py). The platform
# rejects (413) rather than truncates, so an over-cap event would be lost
# entirely unless the SDK trims it first.
MAX_ARGS_BYTES = 8 * 1024

# Mirrors the platform's MAX_ATTRIBUTES_BYTES. Same reject-don't-truncate
# semantics as arguments; smaller because the ABAC bag holds caller facts
# (department, clearance level), not tool payloads.
MAX_ATTRIBUTES_BYTES = 4 * 1024

# Mirrors the platform's MAX_HINT_BYTES. Same reject-don't-truncate semantics.
# Only the audit copy is trimmed — ``Decision.as_error_payload`` still carries
# the intact hint, so the host's file-scope error message keeps its full
# allowed/denied path lists.
MAX_HINT_BYTES = 4 * 1024

# Mirrors the platform's ``DecisionEvent.violations`` bounds (list max_length +
# per-item StringConstraints). A multi-role deny unions one role's violations
# with the next, so a wide caller on the WASM engine can exceed the list cap and
# lose the audit record for a *denied* call — the outcome most worth keeping.
MAX_VIOLATIONS = 64
MAX_VIOLATION_CHARS = 1024

# Caps for LLM message content (scope ``hexgate.messages``), measured on the
# serialized JSON like the decision caps above and enforced twice: here before
# export, and again by the platform's span-enricher. Larger than the decision
# caps because a prompt is not a tool argument. Truncation is head+tail
# (``cap_json_head_tail``) rather than the preview wrapper ``truncate_json``
# uses: on a RAG call the retrieved context sits in the middle of one message,
# and an auditor needs the question at the start and the instruction at the end
# more than the chunks between.
#
# The input cap is 256 KiB, not the 32 KiB first proposed: 32 KiB is ~7,000
# tokens of ASCII, which 20 retrieved chunks already exceed, so the cap would
# have fired on exactly the calls the log exists to explain. What bounds it is
# the pipeline's record and request limits, not storage. Those limits are now
# in the repo — the topic's ``max.message.bytes``, the kafka exporter's
# producer limit, ``send_batch_max_size``, the OTLP receiver's
# body size, the enricher's fetch/produce sizes and this SDK's
# ``MAX_EXPORT_BATCH_SIZE``, all sized together in
# docs/internals/audit-pipeline.md §4.1. Without them a batch of large message
# spans fails as a whole and takes the decision spans batched alongside it down
# too, which is the blast radius these caps exist to bound.
#
# Being in the repo is not the same as being in force: that change is
# operational, so no message cap — 32 KiB or 256 KiB — is safe on a stage until
# its topics have been altered and its collector and enricher restarted.
# The emitter exists (``hexgate.tracing.messages``) but no adapter calls it
# yet, so both stay inert; wiring an adapter hook against a stage that has not
# been redeployed is what is unsafe.
# Typical events stay a few KB; this is a ceiling, not a target.
MAX_INPUT_MESSAGES_BYTES = 256 * 1024
MAX_OUTPUT_MESSAGES_BYTES = 8 * 1024
MAX_SYSTEM_INSTRUCTIONS_BYTES = 8 * 1024

# Keys whose values are stripped from the audit copy of ``arguments`` before
# transmission. Substring match: tool inputs are arbitrary caller data, so a
# key merely *containing* a secret-ish word is worth blanking. A seatbelt, not
# a guarantee: values that are sensitive by content rather than key name (SQL
# strings, email bodies) are NOT caught.
_SENSITIVE_ARG_KEY_RE = re.compile(
    r"password|passwd|secret|token|api[-_]?key|credential|authorization",
    re.IGNORECASE,
)

# Same seatbelt for ``attributes``, but anchored to the whole key. The bag holds
# policy facts, not payloads: ``authorization_tier`` and ``access_token_scope``
# are legitimate ``ctx.*`` keys, and blanking them would leave a ctx-driven deny
# unexplainable — the very thing persisting the bag exists to prevent. A key
# named exactly ``token`` still reads as a secret someone stuffed into the bag,
# so those keep being blanked.
_SENSITIVE_ATTR_KEY_RE = re.compile(
    r"^(?:password|passwd|secret|token|api[-_]?key|credential|authorization)$",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"

# Keys whose string value may itself be serialized JSON that ``_redact`` must
# look inside. In a ``gen_ai.*`` message a tool-call part carries the caller's
# ``arguments``, and the raw OpenAI wire shape serializes them as a JSON
# string, not an object — a string leaf, whose keys the substring match never
# sees. Exact key names on purpose: parsing every string that happens to be
# JSON would rewrite a user message whose content is JSON.
TOOL_CALL_JSON_KEYS = frozenset({"arguments"})


def _redact(
    value: Any,
    *,
    pattern: re.Pattern[str],
    json_string_keys: frozenset[str] = frozenset(),
) -> Any:
    """Return a copy of ``value`` with values under ``pattern``-matching keys replaced.

    Pure — never mutates the input, so the ``Decision`` the caller holds
    keeps its full arguments; only the wire payload is redacted.

    A string value under a key in ``json_string_keys`` that parses to a JSON
    container is redacted inside and serialized back, so the field keeps the
    shape the emitter chose while its nested keys still match."""
    if isinstance(value, dict):
        return {
            k: _REDACTED
            if isinstance(k, str) and pattern.search(k)
            else _redact_json_string(
                v, pattern=pattern, json_string_keys=json_string_keys
            )
            if k in json_string_keys and isinstance(v, str)
            else _redact(v, pattern=pattern, json_string_keys=json_string_keys)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _redact(v, pattern=pattern, json_string_keys=json_string_keys)
            for v in value
        ]
    return value


def _redact_json_string(
    text: str, *, pattern: re.Pattern[str], json_string_keys: frozenset[str]
) -> str:
    """``_redact`` applied inside a JSON-string leaf; non-JSON or scalar JSON
    comes back untouched (nothing in it has a key to match)."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if not isinstance(parsed, (dict, list)):
        return text
    return json.dumps(
        _redact(parsed, pattern=pattern, json_string_keys=json_string_keys)
    )


def _bounded_violations(violations: Sequence[str]) -> list[str]:
    """Trim ``violations`` to the platform's list + per-item caps.

    Drops whole entries and says how many, so a truncated list can't read as a
    complete one. Only the audit copy is trimmed; ``Decision.violations`` keeps
    every entry for the host's error payload.
    """
    trimmed = [
        v if len(v) <= MAX_VIOLATION_CHARS else v[: MAX_VIOLATION_CHARS - 3] + "..."
        for v in violations
    ]
    if len(trimmed) <= MAX_VIOLATIONS:
        return trimmed
    kept = trimmed[: MAX_VIOLATIONS - 1]
    return [*kept, f"(+{len(trimmed) - len(kept)} more)"]


# Room for the {_truncated, original_bytes, preview} wrapper around the preview.
_TRUNCATION_WRAPPER_HEADROOM_BYTES = 512


def _truncate_json(payload: dict[str, Any], *, cap: int) -> dict[str, Any]:
    """Trim ``payload`` to fit a platform byte cap.

    Serialization mirrors the platform's measurement (``default=str``). Over
    the cap, the dict is replaced by a marker wrapping a JSON-text preview,
    shrunk until the wrapper itself fits — lossy, but stored; the platform
    would 413-reject the raw payload and lose the event entirely.

    Under the cap the payload is copied rather than returned as-is, so no wire
    payload aliases a live ``Decision`` field. ``arguments``/``attributes`` get
    that boundary from ``_redact``; ``hint`` has none of its own, and it is the
    same object ``as_error_payload`` hands the host. The copy is shallow —
    enough to stop a rebind, not a nested in-place mutation."""
    payload_json = json.dumps(payload, default=str)
    raw = payload_json.encode("utf-8")
    if len(raw) <= cap:
        return dict(payload)
    # Floored at 1: a cap at or below the wrapper headroom would otherwise make
    # this negative, which slices from the *end* of the payload and never
    # shrinks (``-1 // 2 == -1``), so the loop would not terminate.
    preview_bytes = max(cap - _TRUNCATION_WRAPPER_HEADROOM_BYTES, 1)
    while True:
        wrapper = {
            "_truncated": True,
            "original_bytes": len(raw),
            "preview": raw[:preview_bytes].decode("utf-8", errors="ignore"),
        }
        if len(json.dumps(wrapper).encode("utf-8")) <= cap:
            return wrapper
        if preview_bytes == 1:
            # Not even one byte of preview fits: ship the marker alone.
            return {"_truncated": True, "original_bytes": len(raw)}
        preview_bytes //= 2


# ASCII on purpose: ``json.dumps`` escapes non-ASCII to ``\\uXXXX`` (6 bytes a
# character), so a marker with an ellipsis would cost more than it says.
_HEAD_TAIL_MARKER = " ...[truncated {omitted} bytes]... "
# Below this a string leaf is not worth cutting further: what is over the cap
# is the JSON structure around it, not the text inside it.
_HEAD_TAIL_FLOOR_BYTES = 64


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_head_tail(text: str, *, max_bytes: int) -> str:
    """Shrink ``text`` to at most ``max_bytes`` of UTF-8, keeping its head and
    its tail joined by a marker naming the omitted byte count.

    Returned unchanged when it already fits. Cuts land on code-point
    boundaries (``errors="ignore"`` drops a split character rather than
    emitting a broken one). The marker's room is reserved for the widest count
    it could carry, so the result never exceeds ``max_bytes``."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    reserve = _utf8_len(_HEAD_TAIL_MARKER.format(omitted=len(raw)))
    budget = max_bytes - reserve
    if budget < 2:
        # No room for a marker and content both: keep whatever head fits.
        return raw[:max_bytes].decode("utf-8", errors="ignore")
    head_n = budget // 2
    tail_n = budget - head_n
    head = raw[:head_n].decode("utf-8", errors="ignore")
    tail = raw[-tail_n:].decode("utf-8", errors="ignore")
    omitted = len(raw) - _utf8_len(head) - _utf8_len(tail)
    return head + _HEAD_TAIL_MARKER.format(omitted=omitted) + tail


def _largest_string_leaf_bytes(node: Any) -> int:
    """UTF-8 size of the biggest string value under ``node`` — dict values and
    list items, never dict keys — or ``0`` if there is none."""
    if isinstance(node, str):
        return _utf8_len(node)
    if isinstance(node, dict):
        children: Any = node.values()
    elif isinstance(node, (list, tuple)):
        children = node
    else:
        return 0
    return max((_largest_string_leaf_bytes(child) for child in children), default=0)


def _cap_leaves(node: Any, *, limit: int) -> Any:
    """Rebuild ``node`` with every string leaf shrunk head+tail to ``limit``
    UTF-8 bytes. Leaves already under ``limit`` are kept whole.

    A fresh structure, so the input is never mutated; tuples come back as
    lists, which is what ``json.dumps`` would have written for them anyway and
    what makes the result assignable at all."""
    if isinstance(node, str):
        return _truncate_head_tail(node, max_bytes=limit)
    if isinstance(node, dict):
        return {key: _cap_leaves(child, limit=limit) for key, child in node.items()}
    if isinstance(node, (list, tuple)):
        return [_cap_leaves(child, limit=limit) for child in node]
    return node


def _cap_json_head_tail(value: Any, *, cap: int) -> tuple[Any, bool]:
    """Shrink the string leaves of ``value`` head+tail, largest first, until
    ``json.dumps(value)`` fits ``cap`` bytes. Returns ``(capped, truncated)``.

    Unlike ``_truncate_json`` — which swaps the whole dict for a preview
    wrapper — the result keeps the input's shape (roles, message boundaries,
    part types all survive), so a reader still sees *which* message lost its
    middle. Pure: every path rebuilds the containers rather than writing into
    them, so ``value`` is never mutated. Measurement mirrors the platform
    (``json.dumps(default=str)``), so what is capped here is what the enricher
    measures.

    Every leaf gets the *same* byte allowance, found by binary search on the
    serialized size: the largest allowance that fits. A per-leaf budget derived
    from the overage instead would be wrong twice over — the overage is measured
    on the escaped JSON while a leaf is measured in UTF-8, so escape-heavy
    content (CJK at 6 JSON bytes a character, or anything with many quotes and
    newlines) would over-cut to nothing; and charging one leaf for the whole
    document's overage makes the outcome depend on which message happens to be
    biggest. One allowance for all of them is order-independent and leaves no
    cap unspent.

    Last resort: when the JSON structure alone exceeds ``cap`` (hundreds of
    tiny messages), no string cut can help, so the ``_truncate_json`` preview
    wrapper ships instead — inside a one-item list when the input was a list,
    so the attribute keeps its container type."""
    if _utf8_len(json.dumps(value, default=str)) <= cap:
        # ``limit=cap`` cuts nothing — a leaf cannot exceed a document that
        # fits — so this is the copy, not a truncation. Rebuilding via
        # ``_cap_leaves`` rather than ``copy.deepcopy`` keeps the fast path as
        # tolerant as the slow one: a framework message object holding a lock
        # or a socket is uncopyable, and deep-copying it raised where the
        # truncation path went through.
        return _cap_leaves(value, limit=cap), False
    # Cheapest allowance first: if the floor does not fit, nothing does, and
    # the search below would spend a serialization per step to prove it.
    floored = _cap_leaves(value, limit=_HEAD_TAIL_FLOOR_BYTES)
    if _utf8_len(json.dumps(floored, default=str)) > cap:
        if isinstance(value, list):
            return [_truncate_json({"messages": list(value)}, cap=cap - 2)], True
        payload = value if isinstance(value, dict) else {"value": value}
        return _truncate_json(payload, cap=cap), True
    best, low, high = floored, _HEAD_TAIL_FLOOR_BYTES, _largest_string_leaf_bytes(value)
    while low < high:
        allowance = (low + high + 1) // 2
        candidate = _cap_leaves(value, limit=allowance)
        if _utf8_len(json.dumps(candidate, default=str)) <= cap:
            best, low = candidate, allowance
        else:
            high = allowance - 1
    return best, True


# Public aliases so server-side ingestion can import this pipeline instead of
# keeping its own copy.
redact = _redact
truncate_json = _truncate_json
truncate_head_tail = _truncate_head_tail
cap_json_head_tail = _cap_json_head_tail
bounded_violations = _bounded_violations
SENSITIVE_ARG_KEY_RE = _SENSITIVE_ARG_KEY_RE
SENSITIVE_ATTR_KEY_RE = _SENSITIVE_ATTR_KEY_RE


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Decision plus caller identity from the active HexgateContext scope.

    ``event_id`` / ``occurred_at`` are stamped here, not on ``Decision`` —
    they exist only for audit emission, and the no-audit path never
    constructs an event. ``occurred_at`` becomes the span's start time;
    ``event_id`` is the platform's idempotency key (see ``semconv``)."""

    SCOPE: ClassVar[str] = semconv.SCOPE_AUDIT

    decision: Decision
    user_id: str = ""
    session_id: str = ""
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def span_attributes(self) -> dict[str, Any]:
        """Flat span attributes keyed by ``semconv`` names.

        ``arguments`` and ``attributes`` are redacted (sensitive key names, on
        their own patterns — see ``_SENSITIVE_ATTR_KEY_RE``); those plus
        ``hint`` and ``violations`` are truncated to their platform caps here —
        the single choke point onto the wire. The three dict fields travel as
        JSON strings (the caps are defined in serialized-JSON bytes, so the
        capped quantity stays the measured one); the two list fields as native
        string arrays. Absent optional fields are left out rather than sent as
        ``None`` — OTel attributes can't carry null. The role fields and the
        ``run.*`` fields deliberately pass through untouched — see the comments
        on them below."""
        d = self.decision
        attrs: dict[str, Any] = {
            semconv.EVENT_ID: str(self.event_id),
            semconv.AGENT_NAME: d.agent_name,
            semconv.TOOL_NAME: d.tool_name,
            semconv.OUTCOME: d.outcome.value,
            # Roles evaluated, in caller order, and the one that granted or
            # gated the call ("" on a deny). No legacy scalar ``role``: it was
            # only ever ``user_roles[0]``, and the platform derives what it
            # needs from the list. Uncapped on purpose: these are policy
            # identifiers, not caller payloads, and the platform bounds them
            # (32 x 256) on ``DecisionEvent``.
            semconv.USER_ROLES: list(d.user_roles),
            semconv.DECIDING_ROLE: d.deciding_role or "",
            semconv.ERROR_TYPE: d.error_type or "",
            semconv.REASON: d.reason,
            semconv.VIOLATIONS: _bounded_violations(d.violations),
            semconv.USER_ID: self.user_id,
            semconv.SESSION_ID: self.session_id,
            # Neither redacted nor capped — SDK counters, not caller data.
            # Spread so the wire names live in one place.
            **d.run.as_span_attributes(),
        }
        if d.arguments is not None:
            attrs[semconv.ARGUMENTS] = json.dumps(
                _truncate_json(
                    _redact(d.arguments, pattern=_SENSITIVE_ARG_KEY_RE),
                    cap=MAX_ARGS_BYTES,
                ),
                default=str,
            )
        if d.hint is not None:
            attrs[semconv.HINT] = json.dumps(
                _truncate_json(d.hint, cap=MAX_HINT_BYTES), default=str
            )
        if d.attributes:
            attrs[semconv.ATTRIBUTES] = json.dumps(
                _truncate_json(
                    _redact(d.attributes, pattern=_SENSITIVE_ATTR_KEY_RE),
                    cap=MAX_ATTRIBUTES_BYTES,
                ),
                default=str,
            )
        return attrs


def configure(
    api_key: str | None = None,
    base_url: str | None = None,
) -> AuditSender | None:
    """Get-or-create the audit sender for ``api_key``. Idempotent per key.

    Both args fall back to ``HEXGATE_API_KEY`` / ``HEXGATE_API_URL`` env vars
    (``HEXGATE_OTLP_ENDPOINT`` overrides where spans are exported). Reuses the
    existing sender when the same key was already configured — one sender per
    key carries decisions, LLM usage, bans and LLM messages alike; distinct keys
    get distinct senders. Returns ``None`` when no api_key is resolvable —
    audit stays inert.

    Also returns ``None`` when ``HEXGATE_LOCAL_MODE`` is set in env, even
    if a key was resolvable — that's the "I have a key in .env but I'm
    iterating locally and don't want cloud writes" path
    (``hexgate chat`` opts in via ``bootstrap(local_only=True)``).
    """
    return get_or_create_sender(api_key, base_url)


def get_sender(api_key: str | None = None) -> AuditSender | None:
    """Return the audit sender for ``api_key`` (or ``HEXGATE_API_KEY``), if configured.

    Production code should use the sender injected into
    :class:`~hexgate.security.enforcer.PolicyEnforcer`; this lookup exists for
    diagnostics and is unambiguous only when scoped to a key.
    """
    return _get_sender(api_key)


async def shutdown() -> None:
    """Flush queued events and stop every sender in the shared registry —
    decisions, LLM usage, bans and LLM messages alike. Safe to call multiple
    times."""
    await _shutdown_all()
