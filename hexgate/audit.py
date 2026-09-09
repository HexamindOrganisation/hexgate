"""Per-decision audit emission: one OTel span per decision, exported over
OTLP to the Hexgate Collector under instrumentation scope ``hexgate.audit``.

Fire-and-forget; batched in memory; drops on saturation.
Lifecycle: configure() per api_key, await shutdown() at process exit.
"""

from __future__ import annotations

import copy
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
# caps because a prompt is not a tool argument — 32 KiB is roughly a
# 7,000-token message. Truncation is head+tail (``cap_json_head_tail``) rather
# than the preview wrapper ``truncate_json`` uses: on a RAG call the retrieved
# context sits in the middle of one message, and an auditor needs the question
# at the start and the instruction at the end more than the chunks between.
MAX_INPUT_MESSAGES_BYTES = 32 * 1024
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


def _redact(value: Any, *, pattern: re.Pattern[str]) -> Any:
    """Return a copy of ``value`` with values under ``pattern``-matching keys replaced.

    Pure — never mutates the input, so the ``Decision`` the caller holds
    keeps its full arguments; only the wire payload is redacted."""
    if isinstance(value, dict):
        return {
            k: _REDACTED
            if isinstance(k, str) and pattern.search(k)
            else _redact(v, pattern=pattern)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v, pattern=pattern) for v in value]
    return value


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
    if len(payload_json.encode("utf-8")) <= cap:
        return dict(payload)
    preview_bytes = cap - _TRUNCATION_WRAPPER_HEADROOM_BYTES
    while True:
        wrapper = {
            "_truncated": True,
            "original_bytes": len(payload_json.encode("utf-8")),
            "preview": payload_json.encode("utf-8")[:preview_bytes].decode(
                "utf-8", errors="ignore"
            ),
        }
        if len(json.dumps(wrapper).encode("utf-8")) <= cap:
            return wrapper
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


def _largest_string_leaf(
    node: Any, path: tuple[Any, ...] = ()
) -> tuple[tuple[Any, ...] | None, int]:
    """``(path, utf8_size)`` of the biggest string value under ``node`` — dict
    values and list items, never dict keys — or ``(None, 0)`` if there is none."""
    if isinstance(node, str):
        return path, _utf8_len(node)
    best: tuple[tuple[Any, ...] | None, int] = (None, 0)
    if isinstance(node, dict):
        children = node.items()
    elif isinstance(node, (list, tuple)):
        children = enumerate(node)
    else:
        return best
    for key, child in children:
        found = _largest_string_leaf(child, (*path, key))
        if found[1] > best[1]:
            best = found
    return best


def _get_at(node: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        node = node[key]
    return node


def _set_at(node: Any, path: tuple[Any, ...], value: Any) -> Any:
    if not path:
        return value
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return None


def _cap_json_head_tail(value: Any, *, cap: int) -> tuple[Any, bool]:
    """Shrink the string leaves of ``value`` head+tail, largest first, until
    ``json.dumps(value)`` fits ``cap`` bytes. Returns ``(capped, truncated)``.

    Unlike ``_truncate_json`` — which swaps the whole dict for a preview
    wrapper — the result keeps the input's shape (roles, message boundaries,
    part types all survive), so a reader still sees *which* message lost its
    middle. Pure: ``value`` is deep-copied, never mutated. Measurement mirrors
    the platform (``json.dumps(default=str)``), so what is capped here is
    what the enricher measures.

    Last resort: when the JSON structure alone exceeds ``cap`` (hundreds of
    tiny messages), no string cut can help, so the ``_truncate_json`` preview
    wrapper ships instead — inside a one-item list when the input was a list,
    so the attribute keeps its container type."""
    work = copy.deepcopy(value)
    over = _utf8_len(json.dumps(work, default=str)) - cap
    if over <= 0:
        return work, False
    while over > 0:
        path, size = _largest_string_leaf(work)
        if path is None or size <= _HEAD_TAIL_FLOOR_BYTES:
            if isinstance(work, list):
                return [_truncate_json({"messages": work}, cap=cap - 2)], True
            payload = work if isinstance(work, dict) else {"value": work}
            return _truncate_json(payload, cap=cap), True
        leaf = work if not path else _get_at(work, path)
        # Cutting ``over`` raw bytes removes at least ``over`` JSON bytes (an
        # escaped character encodes to no fewer bytes than its UTF-8), so one
        # pass usually lands under the cap; the loop re-measures regardless.
        target = max(size - over, _HEAD_TAIL_FLOOR_BYTES)
        cut = _truncate_head_tail(leaf, max_bytes=target)
        if not path:
            work = cut
        else:
            _set_at(work, path, cut)
        over = _utf8_len(json.dumps(work, default=str)) - cap
    return work, True


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
        ``None`` — OTel attributes can't carry null. The role fields
        deliberately pass through untouched — see the comment on them below."""
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
    key carries decisions, LLM usage and ban enforcements alike; distinct keys
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
    decisions, LLM usage and ban enforcements alike. Safe to call multiple
    times."""
    await _shutdown_all()
