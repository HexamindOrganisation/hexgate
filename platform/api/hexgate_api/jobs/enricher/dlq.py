"""Dead-letter envelopes for permanently-rejected records and spans.

JSON, not protobuf: the DLQ's consumers are humans (``rpk topic consume``)
and a future replay script, volume is low, and a single span can't be
re-serialized standalone anyway. One message per rejected unit — a span
normally (including every span of a keyless record), the whole record only
when its bytes are undecodable. Keyed by project_id so DLQ partitioning
mirrors the source.

DLQ messages can duplicate whenever a poll replays — a rebalance, a crash, or
a stop during the DLQ retry loop — because offsets commit only after every
send has landed. Envelopes carry no dedup key, so nothing collapses them the
way ReplacingMergeTree does for the tables; consumers must tolerate that.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from opentelemetry.proto.trace.v1.trace_pb2 import Span

from hexgate.audit import (
    SENSITIVE_ARG_KEY_RE,
    TOOL_CALL_JSON_KEYS,
    redact,
    truncate_json,
)
from hexgate.tracing import semconv
from hexgate_api.jobs.enricher.decode import attrs_dict

# The dict fields travel as JSON strings (semconv wire contract). ``redact``
# matches dict keys, so a still-serialized payload would pass through whole
# with its secret-bearing keys unread.
_JSON_DICT_KEYS = (semconv.ARGUMENTS, semconv.HINT, semconv.ATTRIBUTES)
# The gen_ai.* message fields are JSON arrays of messages; a tool-call message
# inside one carries the same caller arguments ``ARGUMENTS`` does, so they are
# parsed and redacted the same way. ``redact`` recurses lists, so an array is
# as redactable as an object — only a bare scalar has nothing to match.
_JSON_LIST_KEYS = (
    semconv.GEN_AI_INPUT_MESSAGES,
    semconv.GEN_AI_OUTPUT_MESSAGES,
    semconv.GEN_AI_SYSTEM_INSTRUCTIONS,
)
# Key → the container types its JSON may parse to and still be redactable.
_JSON_KEYS: dict[str, tuple[type, ...]] = {
    **{key: (dict,) for key in _JSON_DICT_KEYS},
    **{key: (dict, list) for key in _JSON_LIST_KEYS},
}
_UNPARSEABLE = "[UNPARSEABLE]"

# Both variable-size fields are capped well below the DLQ producer's
# max_request_size (``_MAX_RECORD_BYTES``, 8 MiB — and previously aiokafka's
# 1 MiB default, which these caps also cleared): an envelope the producer
# itself cannot send fails client-side on every attempt and would wedge the
# very partition the DLQ exists to protect. Keep them independent of that
# limit rather than scaled to it — the caps are diagnostic previews, not the
# record of truth, and raw span bytes sit on a 30-day topic with no ACLs.
# ``_source`` locates the original bytes while the raw topic's retention lasts.
#
# The two caps never add up: ``span_envelope`` carries the attributes and no
# raw value, ``record_envelope`` the raw value and no attributes. The larger
# shape is the record one, and base64 expands it by a third — 64 KiB of bytes
# ship as ~85 KiB of JSON, which is the real per-envelope worst case.
_ATTRIBUTES_CAP_BYTES = 32 * 1024
_RAW_VALUE_CAP_BYTES = 64 * 1024


def _source(topic: str, partition: int, offset: int) -> dict[str, Any]:
    """Pointer back to the raw record, valid while the source retention lasts."""
    return {"topic": topic, "partition": partition, "offset": offset}


def _redacted_attributes(span: Span) -> dict[str, Any]:
    """Span attributes safe for the DLQ: JSON-string fields parsed, then redacted.

    A field that doesn't parse to the container its contract names can't be
    trusted to redact (``redact`` matches keys, so a bare string has nothing
    to match, and a list where a dict was promised is not the shape the
    emitter meant), so it is dropped rather than forwarded raw — the DLQ is
    for diagnosing the rejection, and the source record (see ``_source``)
    still holds the original bytes.
    """
    attributes = attrs_dict(span.attributes)
    for key, containers in _JSON_KEYS.items():
        raw = attributes.get(key)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            attributes[key] = parsed if isinstance(parsed, containers) else _UNPARSEABLE
    return truncate_json(
        redact(
            attributes,
            pattern=SENSITIVE_ARG_KEY_RE,
            json_string_keys=TOOL_CALL_JSON_KEYS,
        ),
        cap=_ATTRIBUTES_CAP_BYTES,
    )


def span_envelope(
    *,
    error: str,
    error_class: str,
    scope: str,
    project_id: str,
    topic: str,
    partition: int,
    offset: int,
    span: Span,
) -> bytes:
    """Envelope for one rejected span; its siblings are unaffected.

    Attributes are redacted (substring key match, the stricter of the two
    SDK patterns) before they land here: this topic has 30-day retention and
    no ACLs, so unredacted arguments must never reach it. The JSON-string
    fields — decision dicts and gen_ai.* message arrays alike — are parsed
    first so the key match reaches inside them, and they stay parsed in the
    envelope.
    """
    attributes = _redacted_attributes(span)
    payload = {
        "error": error,
        "error_class": error_class,
        "scope": scope,
        "project_id": project_id,
        "source": _source(topic, partition, offset),
        "span": {
            "name": span.name,
            "trace_id_hex": span.trace_id.hex(),
            "span_id_hex": span.span_id.hex(),
            "start_time_unix_nano": span.start_time_unix_nano,
            "attributes": attributes,
        },
    }
    return json.dumps(payload, default=str).encode("utf-8")


def record_envelope(
    *,
    error: str,
    error_class: str,
    project_id: str | None,
    topic: str,
    partition: int,
    offset: int,
    raw_value: bytes,
) -> bytes:
    """Envelope for a whole record that never decoded into spans."""
    payload = {
        "error": error,
        "error_class": error_class,
        "scope": None,
        "project_id": project_id,
        "source": _source(topic, partition, offset),
        "record_value_base64": base64.b64encode(
            raw_value[:_RAW_VALUE_CAP_BYTES]
        ).decode("ascii"),
        "record_value_truncated": len(raw_value) > _RAW_VALUE_CAP_BYTES,
    }
    return json.dumps(payload).encode("utf-8")
