"""enforcement.py — parity with the SDK's as_payload redaction/cap semantics."""

from __future__ import annotations

import json

from hexgate.audit import (
    MAX_INPUT_MESSAGES_BYTES,
    MAX_OUTPUT_MESSAGES_BYTES,
    MAX_SYSTEM_INSTRUCTIONS_BYTES,
    MAX_VIOLATIONS,
)
from hexgate_api.jobs.enricher.enforcement import (
    capped_arguments,
    capped_attributes,
    capped_hint,
    capped_input_messages,
    capped_output_messages,
    capped_system_instructions,
    capped_violations,
)


def test_capped_arguments_happy_path() -> None:
    assert capped_arguments({"query": "hello"}) == {"query": "hello"}
    assert capped_arguments(None) is None


def test_when_argument_key_contains_token_then_redacted() -> None:
    # Substring match: "api_key_id" merely contains a secret-ish word.
    result = capped_arguments({"api_key_id": "abc", "safe": "x"})
    assert result == {"api_key_id": "[REDACTED]", "safe": "x"}


def test_when_attribute_key_is_authorization_tier_then_not_redacted() -> None:
    # Anchored match: policy facts like authorization_tier stay readable;
    # a key named exactly "token" is still blanked.
    result = capped_attributes({"authorization_tier": "gold", "token": "s3cr3t"})
    assert result == {"authorization_tier": "gold", "token": "[REDACTED]"}


def test_when_arguments_exceed_cap_then_truncated_to_marker() -> None:
    result = capped_arguments({"blob": "x" * 20_000})
    assert result["_truncated"] is True
    assert result["original_bytes"] > 8 * 1024
    assert len(json.dumps(result).encode()) <= 8 * 1024


def test_when_hint_over_cap_then_truncated_but_never_redacted() -> None:
    # A hint key containing "token" survives; only the size is enforced.
    small = capped_hint({"token_paths": ["/a"]})
    assert small == {"token_paths": ["/a"]}
    big = capped_hint({"paths": ["x" * 100] * 100})
    assert big["_truncated"] is True


def test_when_attributes_are_an_empty_dict_then_none() -> None:
    assert capped_attributes({}) is None
    assert capped_attributes(None) is None


def test_when_violations_exceed_cap_then_bounded() -> None:
    bounded = capped_violations([f"v{i}" for i in range(MAX_VIOLATIONS + 10)])
    assert len(bounded) == MAX_VIOLATIONS
    assert bounded[-1] == "(+11 more)"


# --- LLM message content -------------------------------------------------------


def _message(content: str, role: str = "user") -> dict:
    return {"role": role, "parts": [{"type": "text", "content": content}]}


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def test_capped_input_messages_happy_path() -> None:
    messages = [_message("hello")]
    text, truncated = capped_input_messages(messages)
    assert json.loads(text) == messages
    assert truncated is False


def test_when_input_messages_exceed_cap_then_head_and_tail_survive() -> None:
    # A RAG-style message: the question up front, the retrieved chunks in the
    # middle, the instruction at the end — the two ends are what an auditor needs.
    messages = [_message("Q" * 1_000 + "C" * 400_000 + "A" * 1_000)]
    text, truncated = capped_input_messages(messages)
    assert truncated is True
    assert _size(text) <= MAX_INPUT_MESSAGES_BYTES
    content = json.loads(text)[0]["parts"][0]["content"]
    assert content.startswith("Q" * 1_000)
    assert content.endswith("A" * 1_000)
    assert "...[truncated " in content


def test_when_input_messages_are_exactly_at_cap_then_untouched() -> None:
    # Boundary: the measured quantity is the serialized JSON, so pad the
    # content until the document is exactly the cap.
    probe = [_message("")]
    overhead = len(json.dumps(probe).encode("utf-8"))
    messages = [_message("x" * (MAX_INPUT_MESSAGES_BYTES - overhead))]
    text, truncated = capped_input_messages(messages)
    assert _size(text) == MAX_INPUT_MESSAGES_BYTES
    assert truncated is False
    assert json.loads(text) == messages


def test_when_input_messages_are_one_byte_over_cap_then_truncated() -> None:
    probe = [_message("")]
    overhead = len(json.dumps(probe).encode("utf-8"))
    messages = [_message("x" * (MAX_INPUT_MESSAGES_BYTES - overhead + 1))]
    text, truncated = capped_input_messages(messages)
    assert truncated is True
    assert _size(text) <= MAX_INPUT_MESSAGES_BYTES


def test_when_a_tool_call_carries_a_secret_argument_then_redacted() -> None:
    # Same rule as decision arguments: a tool-call message holds caller data.
    messages = [
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "name": "login",
                    "arguments": {"user": "bob", "password": "hunter2"},
                }
            ],
        }
    ]
    text, _ = capped_input_messages(messages)
    arguments = json.loads(text)[0]["parts"][0]["arguments"]
    assert arguments == {"user": "bob", "password": "[REDACTED]"}


def test_when_tool_call_arguments_are_a_json_string_then_still_redacted() -> None:
    # Raw OpenAI wire shape: arguments arrive serialized, one JSON level deeper
    # than as_json_value parsed. The secret must not survive inside the string.
    messages = [
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "name": "login",
                    "arguments": json.dumps({"user": "bob", "password": "hunter2"}),
                }
            ],
        }
    ]
    text, _ = capped_input_messages(messages)
    assert "hunter2" not in text
    arguments = json.loads(text)[0]["parts"][0]["arguments"]
    assert json.loads(arguments) == {"user": "bob", "password": "[REDACTED]"}


def test_when_output_messages_exceed_cap_then_truncated_to_8_kib() -> None:
    text, truncated = capped_output_messages([_message("o" * 50_000, "assistant")])
    assert truncated is True
    assert _size(text) <= MAX_OUTPUT_MESSAGES_BYTES


def test_capped_system_instructions_happy_path() -> None:
    parts = [{"type": "text", "content": "You are terse."}]
    text, truncated = capped_system_instructions(parts)
    assert json.loads(text) == parts
    assert truncated is False


def test_when_system_instructions_are_absent_then_empty_string() -> None:
    assert capped_system_instructions(None) == ("", False)


def test_when_system_instructions_exceed_cap_then_truncated_to_8_kib() -> None:
    text, truncated = capped_system_instructions(
        [{"type": "text", "content": "s" * 50_000}]
    )
    assert truncated is True
    assert _size(text) <= MAX_SYSTEM_INSTRUCTIONS_BYTES
