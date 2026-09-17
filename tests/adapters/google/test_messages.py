"""``Content``/``Part`` → OTel GenAI messages: the pure conversion layer in
``hexgate.adapters.google.messages``. No hooks, no emission — what goes in is
what ADK hands a callback, what comes out is what lands in the ``llm_message``
content columns."""

from __future__ import annotations

from google.genai import types

from hexgate.adapters.google.messages import (
    input_message,
    output_messages,
    system_parts,
)


def _text(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


# --- Input conversion ---------------------------------------------------------


def test_input_message_happy_path() -> None:
    assert input_message(_text("Hi")) == {
        "role": "user",
        "parts": [{"type": "text", "content": "Hi"}],
    }


def test_when_role_is_model_then_it_becomes_assistant() -> None:
    content = types.Content(role="model", parts=[types.Part(text="Sunny, 24C.")])

    assert input_message(content)["role"] == "assistant"


def test_when_content_holds_a_function_call_then_it_becomes_a_tool_call_part() -> None:
    content = types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    id="call_1", name="get_weather", args={"city": "Paris"}
                )
            )
        ],
    )

    assert input_message(content) == {
        "role": "assistant",
        "parts": [
            {
                "type": "tool_call",
                "id": "call_1",
                "name": "get_weather",
                "arguments": {"city": "Paris"},
            }
        ],
    }


def test_when_content_holds_a_function_response_then_the_role_is_tool() -> None:
    """ADK files a tool result under ``role="user"`` because that is what the
    Gemini API wants; GenAI calls that turn ``tool``."""
    content = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id="call_1", name="get_weather", response={"result": "Sunny, 24C."}
                )
            )
        ],
    )

    assert input_message(content) == {
        "role": "tool",
        "parts": [
            {
                "type": "tool_call_response",
                "id": "call_1",
                "name": "get_weather",
                "response": {"result": "Sunny, 24C."},
            }
        ],
    }


def test_when_a_part_has_no_genai_equivalent_then_it_is_carried_through() -> None:
    content = types.Content(
        role="user",
        parts=[
            types.Part(
                inline_data=types.Blob(mime_type="image/png", data=b"\x89PNG"),
            )
        ],
    )

    [part] = input_message(content)["parts"]
    # Tagged here, not by google-genai: a Part is a union by populated field.
    assert part["type"] == "inline_data"
    assert part["inline_data"]["mime_type"] == "image/png"


def test_when_an_input_part_is_a_thought_then_it_is_marked_as_reasoning() -> None:
    """ADK carries a thought forward in the history, where flattening it to
    text would read as the answer the model gave."""
    content = types.Content(
        role="model",
        parts=[
            types.Part(text="Let me think.", thought=True),
            types.Part(text="Sunny, 24C."),
        ],
    )

    assert input_message(content)["parts"] == [
        {"type": "reasoning", "content": "Let me think."},
        {"type": "text", "content": "Sunny, 24C."},
    ]


def test_when_content_has_no_parts_then_the_message_is_empty() -> None:
    assert input_message(types.Content(role="user")) == {"role": "user", "parts": []}


# --- Output conversion --------------------------------------------------------


def test_output_messages_happy_path() -> None:
    content = types.Content(role="model", parts=[types.Part(text="Sunny, 24C.")])

    assert output_messages(content) == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Sunny, 24C."}]}
    ]


def test_when_response_has_text_and_a_tool_call_then_they_share_one_message() -> None:
    """One model call is one completion, whatever the API splits it into."""
    content = types.Content(
        role="model",
        parts=[
            types.Part(text="Checking."),
            types.Part(
                function_call=types.FunctionCall(name="get_weather", args={"city": "P"})
            ),
        ],
    )

    [message] = output_messages(content)
    assert [part["type"] for part in message["parts"]] == ["text", "tool_call"]


def test_when_a_part_is_a_thought_then_it_is_dropped() -> None:
    content = types.Content(
        role="model",
        parts=[
            types.Part(text="Let me think.", thought=True),
            types.Part(text="Sunny, 24C."),
        ],
    )

    assert output_messages(content) == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Sunny, 24C."}]}
    ]


def test_when_there_is_no_content_then_there_are_no_output_messages() -> None:
    assert output_messages(None) == []
    assert output_messages(types.Content(role="model", parts=[])) == []


# --- System instructions ------------------------------------------------------


def test_system_parts_happy_path() -> None:
    assert system_parts("You are a weather assistant.") == [
        {"type": "text", "content": "You are a weather assistant."}
    ]


def test_when_system_instruction_is_a_content_then_its_parts_are_converted() -> None:
    instruction = types.Content(parts=[types.Part(text="Be terse.")])

    assert system_parts(instruction) == [{"type": "text", "content": "Be terse."}]


def test_when_system_instruction_is_a_list_then_the_parts_are_concatenated() -> None:
    assert system_parts(["Be terse.", types.Part(text="Be kind.")]) == [
        {"type": "text", "content": "Be terse."},
        {"type": "text", "content": "Be kind."},
    ]


def test_when_there_is_no_system_instruction_then_there_are_no_parts() -> None:
    assert system_parts(None) is None
