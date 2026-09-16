"""Responses-API items → OTel GenAI messages: the pure conversion layer in
``hexgate.adapters.openai.messages``. No hooks, no emission — what goes in is
what the SDK hands a callback, what comes out is what lands in the
``llm_message`` content columns."""

from __future__ import annotations

from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
)
from openai.types.responses.response_reasoning_item import Summary

from hexgate.adapters.openai.messages import input_message, output_messages


def _text_output(text: str = "Sunny, 24C.") -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg_1",
        role="assistant",
        status="completed",
        type="message",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def _tool_call_output(
    name: str = "get_weather", arguments: str = '{"city": "Paris"}'
) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id="fc_1",
        call_id="call_1",
        name=name,
        arguments=arguments,
        type="function_call",
        status="completed",
    )


# --- Input-item conversion ----------------------------------------------------


def test_input_message_happy_path() -> None:
    assert input_message({"role": "user", "content": "Hi"}) == {
        "role": "user",
        "parts": [{"type": "text", "content": "Hi"}],
    }


def test_when_content_is_a_part_list_then_text_parts_are_flattened() -> None:
    """``input_text``/``output_text`` are the Responses API's two names for
    the same thing; GenAI has one ``text`` part."""
    item = {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "What is in this?"},
            {"type": "input_image", "image_url": "https://example.test/a.png"},
        ],
    }

    assert input_message(item) == {
        "role": "user",
        "parts": [
            {"type": "text", "content": "What is in this?"},
            {"type": "input_image", "image_url": "https://example.test/a.png"},
        ],
    }


def test_when_item_is_a_function_call_then_it_becomes_a_tool_call_part() -> None:
    """``arguments`` stays the raw JSON string the API uses, so redaction's
    ``TOOL_CALL_JSON_KEYS`` rule opens it and blanks the keys inside."""
    item = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "get_weather",
        "arguments": '{"city": "Paris"}',
    }

    assert input_message(item) == {
        "role": "assistant",
        "parts": [
            {
                "type": "tool_call",
                "id": "call_1",
                "name": "get_weather",
                "arguments": '{"city": "Paris"}',
            }
        ],
    }


def test_when_item_is_a_function_call_output_then_it_becomes_a_tool_response() -> None:
    """The only place a tool's return value is stored: decision events record
    the call, never its result."""
    item = {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "sunny, 22C",
    }

    assert input_message(item) == {
        "role": "tool",
        "parts": [
            {"type": "tool_call_response", "id": "call_1", "response": "sunny, 22C"}
        ],
    }


def test_when_item_has_no_role_or_known_type_then_it_is_carried_through() -> None:
    """Reasoning items and built-in tool calls have no GenAI part to map onto;
    keeping them whole leaves the turn complete instead of holed."""
    item = {"type": "reasoning", "id": "rs_1", "summary": []}

    assert input_message(item) == {"role": "assistant", "parts": [item]}


# --- Output conversion --------------------------------------------------------


def test_output_messages_happy_path() -> None:
    assert output_messages([_text_output("Sunny, 24C.")]) == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Sunny, 24C."}]}
    ]


def test_when_response_has_text_and_a_tool_call_then_they_share_one_message() -> None:
    """One model call is one completion; the Responses API just splits it
    across items."""
    assert output_messages([_text_output("Checking."), _tool_call_output()]) == [
        {
            "role": "assistant",
            "parts": [
                {"type": "text", "content": "Checking."},
                {
                    "type": "tool_call",
                    "id": "call_1",
                    "name": "get_weather",
                    "arguments": '{"city": "Paris"}',
                },
            ],
        }
    ]


def test_when_response_output_is_empty_then_there_are_nooutput_messages() -> None:
    assert output_messages([]) == []


def test_when_output_is_only_a_reasoning_item_then_no_message_is_emitted() -> None:
    """Reasoning is dropped (issue #221): every part of the output message
    shares one 8 KiB budget sized for a text answer, and reasoning leads the
    output, so head+tail truncation would cut the answer rather than the
    reasoning. Nothing else in this item is kept — dropping it leaves no
    parts, and no message."""
    reasoning = ResponseReasoningItem(
        id="rs_1",
        type="reasoning",
        summary=[Summary(type="summary_text", text="Check the weather first.")],
    )

    assert output_messages([reasoning]) == []


def test_when_a_reasoning_item_carries_content_then_it_is_dropped_too() -> None:
    """On the Chat Completions path (LiteLLM, Anthropic thinking blocks) the
    SDK populates ``content`` on a reasoning item, so a branch that routed on
    that key would have emitted the reasoning text and silently dropped the
    ``summary`` beside it. Typing the item is what makes both paths agree."""
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "provider reasoning"}],
        "content": [{"type": "reasoning_text", "text": "thinking block"}],
        "encrypted_content": "sig123",
    }

    assert output_messages([reasoning]) == []


def test_when_output_mixes_reasoning_and_an_answer_then_only_the_answer_is_kept() -> (
    None
):
    """The realistic shape: a reasoning item leads, the answer follows. The
    answer keeps the whole budget, which is the point of dropping."""
    reasoning = ResponseReasoningItem(
        id="rs_1",
        type="reasoning",
        summary=[Summary(type="summary_text", text="Check the weather first.")],
    )
    answer = ResponseOutputMessage(
        id="msg_1",
        role="assistant",
        status="completed",
        type="message",
        content=[
            ResponseOutputText(type="output_text", text="Sunny, 24C.", annotations=[])
        ],
    )

    [message] = output_messages([reasoning, answer])

    assert message["parts"] == [{"type": "text", "content": "Sunny, 24C."}]
