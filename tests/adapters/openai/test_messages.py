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


def test_when_output_is_a_reasoning_item_then_its_summary_is_kept() -> None:
    """A reasoning item declares a ``content`` field that is None and keeps
    its text under ``summary``. Routing on the key's presence would emit an
    empty message and lose the last turn's reasoning for good — earlier turns
    come back in the next call's input delta, the final one never does."""
    reasoning = ResponseReasoningItem(
        id="rs_1",
        type="reasoning",
        summary=[Summary(type="summary_text", text="Check the weather first.")],
    )

    [message] = output_messages([reasoning])

    assert message["role"] == "assistant"
    [part] = message["parts"]
    assert part["summary"] == [
        {"type": "summary_text", "text": "Check the weather first."}
    ]
