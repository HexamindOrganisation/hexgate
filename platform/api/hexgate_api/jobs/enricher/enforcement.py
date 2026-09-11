"""Authoritative server-side redaction + byte caps for decision payloads.

Same pipeline the SDK applies client-side (imported from hexgate.audit, not
copied — two implementations would drift), but here it is enforcement: the
batch inserts trust their input and never re-check, so this is the single
point where oversized or secret-bearing payloads get trimmed. Order and
gating mirror ``AuditEvent.as_payload``: arguments are redacted (substring
key match) then capped at 8 KiB; hint is capped at 4 KiB but never redacted
(policy config, not caller data); attributes are redacted (anchored key
match) then capped at 4 KiB, with a falsy bag normalised to None.

LLM message content (scope ``hexgate.messages``) follows the arguments rule
— substring key redaction, since a tool-call message carries the same caller
arguments a decision does, reaching inside ``arguments`` serialized as a JSON
string the way the raw OpenAI wire shape has it — then a head+tail cap per
field. The SDK applied
the same caps before export; re-applying them here is what makes the stored
row's size a platform guarantee rather than a client courtesy. Content is
truncated, never rejected: an over-cap prompt is the one an auditor most
needs to see.
"""

from __future__ import annotations

import json
from typing import Any

from hexgate.audit import (
    MAX_ARGS_BYTES,
    MAX_ATTRIBUTES_BYTES,
    MAX_HINT_BYTES,
    MAX_INPUT_MESSAGES_BYTES,
    MAX_OUTPUT_MESSAGES_BYTES,
    MAX_SYSTEM_INSTRUCTIONS_BYTES,
    SENSITIVE_ARG_KEY_RE,
    SENSITIVE_ATTR_KEY_RE,
    TOOL_CALL_JSON_KEYS,
    bounded_violations,
    cap_json_head_tail,
    redact,
    truncate_json,
)


def capped_arguments(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return truncate_json(
        redact(value, pattern=SENSITIVE_ARG_KEY_RE), cap=MAX_ARGS_BYTES
    )


def capped_hint(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return truncate_json(value, cap=MAX_HINT_BYTES)


def capped_attributes(value: dict[str, Any] | None) -> dict[str, Any] | None:
    # Falsy, not ``is not None`` — {} and None both store as absent downstream.
    if not value:
        return None
    return truncate_json(
        redact(value, pattern=SENSITIVE_ATTR_KEY_RE), cap=MAX_ATTRIBUTES_BYTES
    )


def capped_violations(values: list[str]) -> list[str]:
    return bounded_violations(values)


def _capped_content(value: Any, *, cap: int) -> tuple[str, bool]:
    """Redact, cap head+tail, and serialize one ``gen_ai.*`` content field.

    Returns the JSON text the row stores and whether this side cut it — the
    caller ORs that into the SDK's ``truncated`` flag. Serialization matches
    the cap's own measure (``json.dumps``, ``default=str``), so the text is
    never longer than what was found to fit.
    """
    capped, truncated = cap_json_head_tail(
        redact(
            value, pattern=SENSITIVE_ARG_KEY_RE, json_string_keys=TOOL_CALL_JSON_KEYS
        ),
        cap=cap,
    )
    return json.dumps(capped, default=str), truncated


def capped_input_messages(value: Any) -> tuple[str, bool]:
    return _capped_content(value, cap=MAX_INPUT_MESSAGES_BYTES)


def capped_output_messages(value: Any) -> tuple[str, bool]:
    return _capped_content(value, cap=MAX_OUTPUT_MESSAGES_BYTES)


def capped_system_instructions(value: Any | None) -> tuple[str, bool]:
    # Only the first event of a turn_key carries instructions; the column
    # defaults to "" for the rest.
    if value is None:
        return "", False
    return _capped_content(value, cap=MAX_SYSTEM_INSTRUCTIONS_BYTES)
