"""Semantic conventions for Hexgate audit events carried as OTel spans.

Single source of truth for the OTLP wire contract between the SDK's span
emitter (``hexgate.tracing._senders``) and the platform's span-enricher job. Both sides import
these names; neither hardcodes a string. String constants only — this
module must stay importable with zero dependencies.

Wire contract (normative — the enricher decodes by these rules, the emitter
must produce by them):

- One event = one span. The instrumentation scope name selects the event
  type: ``SCOPE_AUDIT`` → DecisionEvent, ``SCOPE_USAGE`` → LlmInvocationEvent,
  ``SCOPE_BANS`` → BanEnforcementEvent, ``SCOPE_MESSAGES`` → LlmMessageEvent
  (platform schemas).
- ``occurred_at`` travels as the span's ``start_time_unix_nano`` — the one
  field OTLP already types as a timestamp. No separate attribute: a duplicate
  would invite the two values disagreeing with no rule for which wins. These
  are point-in-time events; the emitter sets start == end (or start ≈ end).
  A zero start time is a rejected span.
- ``event_id`` travels as the ``EVENT_ID`` string-UUID attribute, never as
  span identity: ``span_id`` is 8 bytes and regenerates on a client retry,
  so keying dedup on it would fail to collapse retries. ``EVENT_ID`` is the
  ReplacingMergeTree idempotency key end-to-end.
- ``project_id`` is NOT a span attribute. It is auth-derived by the
  Collector's Biscuit extension and travels as the Kafka record key —
  a self-declared project on the span body is never trusted.
- List fields (``USER_ROLES``, ``VIOLATIONS``) are native OTLP string
  arrays (AnyValue.array_value).
- Dict fields (``HINT``, ``ARGUMENTS``, ``ATTRIBUTES``) are JSON-string
  attributes, not kvlists and not flattened keys: their platform byte caps
  are defined in serialized-JSON bytes, so a JSON string keeps the capped
  quantity the measured quantity, and kvlist round-tripping is the least
  supported AnyValue shape across third-party pipelines.
- LLM usage reuses the official ``gen_ai.*`` names where they exist (model,
  token counts) and never invents new ``gen_ai.*`` names; everything
  Hexgate-specific lives under ``sec_ai.*``.
- LLM message content (``SCOPE_MESSAGES``) is the official GenAI trio —
  ``gen_ai.input.messages`` / ``gen_ai.output.messages`` /
  ``gen_ai.system_instructions`` — each a JSON-string attribute (same rule as
  the dict fields above: the platform caps are measured in serialized-JSON
  bytes, and the SDK applies the same caps before export). One event per LLM
  call carrying only the input messages *new to that call* plus its
  completion, never a snapshot of the whole conversation. ``MESSAGE_SEQ``
  counts events within one ``TURN_KEY`` (one framework message list — a
  sub-agent or handoff has its own) so a reader can detect a missing row;
  ``RESYNCED`` marks an event that restates the whole list because the
  framework rewrote it rather than extending it. ``TRUNCATED`` is set by
  the SDK when a cap cut any content field.
"""

from __future__ import annotations

# --- Instrumentation scope names (one per event stream) -----------------------
SCOPE_AUDIT = "hexgate.audit"
SCOPE_USAGE = "hexgate.usage"
SCOPE_BANS = "hexgate.bans"
SCOPE_MESSAGES = "hexgate.messages"

# --- Envelope attributes (all four scopes) ------------------------------------
EVENT_ID = "sec_ai.event_id"
AGENT_NAME = "sec_ai.agent_name"
SESSION_ID = "sec_ai.session_id"
USER_ID = "sec_ai.user_id"

# --- Decision spans (SCOPE_AUDIT) ----------------------------------------------
TOOL_NAME = "sec_ai.tool_name"
OUTCOME = "sec_ai.outcome"
USER_ROLES = "sec_ai.user_roles"
DECIDING_ROLE = "sec_ai.deciding_role"
ERROR_TYPE = "sec_ai.error_type"
REASON = "sec_ai.reason"
VIOLATIONS = "sec_ai.violations"
HINT = "sec_ai.hint"
ARGUMENTS = "sec_ai.arguments"
ATTRIBUTES = "sec_ai.attributes"

# --- Run attribution (SCOPE_AUDIT; RUN_ID also on SCOPE_USAGE, SCOPE_MESSAGES) --
# Omitted entirely when the emitter has no run to attribute: OTLP attributes
# cannot carry null, and the platform's run_id is ``UUID | None`` — an empty
# string is a 422. An absent attribute decodes to None; "" would not.
RUN_ID = "sec_ai.run_id"
RUN_TOOL_CALLS = "sec_ai.run_tool_calls"
RUN_LLM_CALLS = "sec_ai.run_llm_calls"
RUN_DENIALS = "sec_ai.run_denials"
RUN_TOTAL_TOKENS = "sec_ai.run_total_tokens"
RUN_ELAPSED_MS = "sec_ai.run_elapsed_ms"

# --- LLM usage spans (SCOPE_USAGE) ----------------------------------------------
# Official OTel GenAI semconv names — never coin new gen_ai.* names ourselves.
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
# Hexgate-specific usage fields.
LATENCY_MS = "sec_ai.latency_ms"
STATUS = "sec_ai.status"
ERROR_CODE = "sec_ai.error_code"

# --- Ban enforcement spans (SCOPE_BANS) ------------------------------------------
BAN_TYPE = "sec_ai.ban_type"
BAN_ID = "sec_ai.ban_id"

# --- LLM message spans (SCOPE_MESSAGES) -------------------------------------------
# Official OTel GenAI semconv names for prompt/completion content — verbatim.
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
# Hexgate-specific message fields (see the wire contract above).
MESSAGE_SEQ = "sec_ai.message_seq"
TURN_KEY = "sec_ai.turn_key"
RESYNCED = "sec_ai.resynced"
TRUNCATED = "sec_ai.truncated"
