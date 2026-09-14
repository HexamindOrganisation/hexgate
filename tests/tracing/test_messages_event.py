"""LlmMessageEvent.span_attributes() — the message span's attribute layout per
``hexgate.tracing.semconv``: official ``gen_ai.*`` names for model and
content, ``sec_ai.*`` for the envelope and the transcript bookkeeping, with
the SDK-side caps and redaction applied on the way out."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from hexgate.audit import (
    MAX_INPUT_MESSAGES_BYTES,
    MAX_OUTPUT_MESSAGES_BYTES,
    MAX_SYSTEM_INSTRUCTIONS_BYTES,
)
from hexgate.tracing import semconv
from hexgate.tracing.messages import LlmMessageEvent


def _message(content: str, role: str = "user") -> dict[str, Any]:
    return {"role": role, "parts": [{"type": "text", "content": content}]}


def _event(**overrides: Any) -> LlmMessageEvent:
    base: dict[str, Any] = dict(
        agent_name="example_agent",
        model="gpt-4o",
        input_messages=[_message("What is the weather in Paris?")],
        output_messages=[_message("Sunny, 24°C.", role="assistant")],
        turn_key="run-1",
        message_seq=0,
    )
    return LlmMessageEvent(**{**base, **overrides})


def _json_size(text: str) -> int:
    return len(text.encode("utf-8"))


def test_span_attributes_happy_path() -> None:
    ev = _event(
        session_id="sess_1",
        user_id="alice",
        system_instructions=[{"type": "text", "content": "Be brief."}],
        message_seq=3,
        resynced=True,
    )
    wire = ev.span_attributes()

    assert LlmMessageEvent.SCOPE == semconv.SCOPE_MESSAGES
    assert wire[semconv.EVENT_ID] == str(ev.event_id)
    assert wire[semconv.AGENT_NAME] == "example_agent"
    assert wire[semconv.SESSION_ID] == "sess_1"
    assert wire[semconv.USER_ID] == "alice"
    assert wire[semconv.GEN_AI_REQUEST_MODEL] == "gpt-4o"
    assert json.loads(wire[semconv.GEN_AI_INPUT_MESSAGES]) == ev.input_messages
    assert json.loads(wire[semconv.GEN_AI_OUTPUT_MESSAGES]) == ev.output_messages
    assert (
        json.loads(wire[semconv.GEN_AI_SYSTEM_INSTRUCTIONS]) == ev.system_instructions
    )
    assert wire[semconv.TURN_KEY] == "run-1"
    assert wire[semconv.MESSAGE_SEQ] == 3
    assert wire[semconv.RESYNCED] is True
    assert wire[semconv.TRUNCATED] is False
    # occurred_at is the span start time; project_id and received_at are
    # server-resolved — none of them is an attribute.
    assert not any("occurred_at" in k for k in wire)
    assert not any("project_id" in k for k in wire)
    assert not any("received_at" in k for k in wire)


def test_span_attributes_content_fields_are_json_strings() -> None:
    """The wire contract carries the GenAI trio as JSON strings — never as
    kvlists or flattened keys — so the platform's byte caps measure the same
    thing the SDK capped."""
    wire = _event(system_instructions=[]).span_attributes()
    for key in (
        semconv.GEN_AI_INPUT_MESSAGES,
        semconv.GEN_AI_OUTPUT_MESSAGES,
        semconv.GEN_AI_SYSTEM_INSTRUCTIONS,
    ):
        assert type(wire[key]) is str
        json.loads(wire[key])


def test_span_attributes_bookkeeping_types_match_the_contract() -> None:
    """The wire contract types ``message_seq`` as int and the two flags as
    bool. The OTLP encoder writes ``bool_value`` only for a Python bool, so a
    stringified ``"0"`` or ``"false"`` would travel as a string instead."""
    wire = _event().span_attributes()
    assert type(wire[semconv.MESSAGE_SEQ]) is int
    assert type(wire[semconv.RESYNCED]) is bool
    assert type(wire[semconv.TRUNCATED]) is bool


def test_span_attributes_when_system_instructions_are_none_then_the_key_is_absent() -> (
    None
):
    """Only the first event of a ``turn_key`` carries the instructions. The
    others leave the attribute out entirely: OTel cannot carry null, and ``""``
    is not valid JSON where the contract promises a JSON string — genuinely
    empty instructions travel as ``[]``."""
    wire = _event(system_instructions=None).span_attributes()
    assert semconv.GEN_AI_SYSTEM_INSTRUCTIONS not in wire


def test_event_id_defaults_to_a_stringified_uuid_and_is_unique_per_event() -> None:
    ev1, ev2 = _event(), _event()

    assert ev1.event_id != ev2.event_id
    assert ev1.span_attributes()[semconv.EVENT_ID] == str(ev1.event_id)


def test_occurred_at_defaults_to_an_aware_utc_datetime() -> None:
    ev = _event()
    assert ev.occurred_at.utcoffset() is not None
    assert ev.occurred_at.utcoffset().total_seconds() == 0


def test_span_attributes_omit_run_id_never_send_an_empty_string() -> None:
    """An empty string is not a UUID: the enricher DLQs the span, losing the
    transcript row for every model call made outside a run scope."""
    assert semconv.RUN_ID not in _event().span_attributes()


def test_span_attributes_carry_the_run_id_when_inside_a_run() -> None:
    wire = _event(run_id="9c2f1d3e-0000-4000-8000-000000000001").span_attributes()

    assert wire[semconv.RUN_ID] == "9c2f1d3e-0000-4000-8000-000000000001"


# ---------------------------------------------------------------------------
# caps: applied SDK-side, head+tail, flagged once
# ---------------------------------------------------------------------------


def test_when_input_exceeds_its_cap_then_it_is_cut_head_tail_and_flagged() -> None:
    """A RAG prompt stuffs every retrieved chunk into one message. The start
    (the question) and the end (the instruction) both survive the cut, the
    field fits the cap, and ``truncated`` tells the reader the row is lossy."""
    big = "Q" * 1_000 + "C" * (2 * MAX_INPUT_MESSAGES_BYTES) + "A" * 1_000
    wire = _event(input_messages=[_message(big)]).span_attributes()

    payload = wire[semconv.GEN_AI_INPUT_MESSAGES]
    assert _json_size(payload) <= MAX_INPUT_MESSAGES_BYTES
    [msg] = json.loads(payload)
    content = msg["parts"][0]["content"]
    assert content.startswith("QQQ")
    assert content.endswith("AAA")
    assert "...[truncated " in content
    assert wire[semconv.TRUNCATED] is True


def test_when_output_exceeds_its_cap_then_only_that_field_is_cut() -> None:
    wire = _event(
        output_messages=[_message("X" * (3 * MAX_OUTPUT_MESSAGES_BYTES), "assistant")]
    ).span_attributes()

    assert _json_size(wire[semconv.GEN_AI_OUTPUT_MESSAGES]) <= MAX_OUTPUT_MESSAGES_BYTES
    assert json.loads(wire[semconv.GEN_AI_INPUT_MESSAGES]) == _event().input_messages
    assert wire[semconv.TRUNCATED] is True


def test_when_system_instructions_exceed_their_cap_then_truncated_is_set() -> None:
    wire = _event(
        system_instructions=[
            {"type": "text", "content": "S" * (3 * MAX_SYSTEM_INSTRUCTIONS_BYTES)}
        ]
    ).span_attributes()

    assert (
        _json_size(wire[semconv.GEN_AI_SYSTEM_INSTRUCTIONS])
        <= MAX_SYSTEM_INSTRUCTIONS_BYTES
    )
    assert wire[semconv.TRUNCATED] is True


def test_span_attributes_do_not_mutate_the_messages_they_were_given() -> None:
    """The adapter still holds the framework's list after the emit — for the
    delta cursor's fingerprint, if nothing else — so the cap and the redaction
    must work on copies."""
    big = [_message("C" * (2 * MAX_INPUT_MESSAGES_BYTES))]
    tool_call = [
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "name": "db",
                    "arguments": {"query": "select 1", "api_key": "sk-live"},
                }
            ],
        }
    ]
    before_big, before_tool = copy.deepcopy(big), copy.deepcopy(tool_call)

    _event(input_messages=big, output_messages=tool_call).span_attributes()

    assert big == before_big
    assert tool_call == before_tool


# ---------------------------------------------------------------------------
# redaction: the tool arguments inside a prompt follow the decision's rule
# ---------------------------------------------------------------------------


def test_when_a_tool_call_carries_a_secret_argument_then_it_is_redacted() -> None:
    """A decision event blanks ``api_key`` inside ``arguments``; the assistant
    message that requested the same tool call carries the same arguments, so
    leaving them readable here would undo that redaction."""
    tool_call = {
        "role": "assistant",
        "parts": [
            {
                "type": "tool_call",
                "name": "db",
                "arguments": {"query": "select 1", "api_key": "sk-live-123"},
            }
        ],
    }
    wire = _event(output_messages=[tool_call]).span_attributes()

    [msg] = json.loads(wire[semconv.GEN_AI_OUTPUT_MESSAGES])
    args = msg["parts"][0]["arguments"]
    assert args["api_key"] == "[REDACTED]"
    assert args["query"] == "select 1"


def test_when_tool_call_arguments_are_a_json_string_then_secrets_inside_are_redacted() -> (
    None
):
    """The raw OpenAI wire shape serialises ``arguments`` as a JSON string, one
    level below what key matching sees; the SDK parses that string exactly as
    the enricher does, so the two ends blank the same keys."""
    tool_call = {
        "role": "assistant",
        "parts": [
            {
                "type": "tool_call",
                "name": "db",
                "arguments": json.dumps({"query": "select 1", "api_key": "sk-live"}),
            }
        ],
    }
    wire = _event(output_messages=[tool_call]).span_attributes()

    [msg] = json.loads(wire[semconv.GEN_AI_OUTPUT_MESSAGES])
    args = json.loads(msg["parts"][0]["arguments"])
    assert args == {"query": "select 1", "api_key": "[REDACTED]"}


class _ToolCallPart(BaseModel):
    type: str = "tool_call"
    name: str
    arguments: dict[str, Any]


class _Message(BaseModel):
    role: str
    parts: list[_ToolCallPart]


def test_when_messages_are_pydantic_models_then_they_are_unwrapped_and_redacted() -> (
    None
):
    """Every supported framework's message type is a pydantic model. Left as
    an object it would be stored as a ``repr()`` with the secret inside;
    unwrapped first, the same key rule reaches it."""
    msg = _Message(
        role="assistant",
        parts=[_ToolCallPart(name="db", arguments={"q": "select 1", "token": "t-1"})],
    )
    wire = _event(output_messages=[msg]).span_attributes()

    [stored] = json.loads(wire[semconv.GEN_AI_OUTPUT_MESSAGES])
    assert stored["role"] == "assistant"
    assert stored["parts"][0]["arguments"] == {"q": "select 1", "token": "[REDACTED]"}


def test_when_a_message_is_a_dataclass_then_it_is_unwrapped_and_redacted() -> None:
    @dataclass
    class Part:
        type: str
        arguments: dict[str, Any]

    @dataclass
    class Msg:
        role: str
        parts: list[Part]

    msg = Msg(role="assistant", parts=[Part("tool_call", {"api_key": "sk-live"})])
    wire = _event(output_messages=[msg]).span_attributes()

    [stored] = json.loads(wire[semconv.GEN_AI_OUTPUT_MESSAGES])
    assert stored["parts"][0]["arguments"] == {"api_key": "[REDACTED]"}


def test_when_a_leaf_is_an_arbitrary_object_then_it_is_stringified_not_dropped() -> (
    None
):
    class Opaque:
        def __str__(self) -> str:
            return "<opaque>"

    wire = _event(
        input_messages=[{"role": "user", "parts": [Opaque()]}]
    ).span_attributes()

    [stored] = json.loads(wire[semconv.GEN_AI_INPUT_MESSAGES])
    assert stored["parts"] == ["<opaque>"]


def test_redaction_leaves_message_shape_keys_and_text_content_alone() -> None:
    """Substring matching on key names must not touch ``role``/``parts``/
    ``type``/``content``, and never the text itself — a user talking *about*
    a password is not a secret by key name."""
    text = "I forgot my password, can you reset the token?"
    wire = _event(input_messages=[_message(text)]).span_attributes()

    assert json.loads(wire[semconv.GEN_AI_INPUT_MESSAGES]) == [_message(text)]
