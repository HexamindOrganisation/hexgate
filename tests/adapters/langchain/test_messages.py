"""Tests for the LangChain ``BaseMessage`` → GenAI ``role``/``parts``
converters. Pure functions over plain dicts — no handler, no emit."""

from __future__ import annotations

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    ChatMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    ToolMessageChunk,
)
from langchain_core.outputs import ChatGeneration, Generation, LLMResult

from hexgate.adapters.langchain.messages import (
    input_message,
    output_messages,
    split_system_instructions,
    text_part,
)

# --- input_message ------------------------------------------------------------


def test_input_message_happy_path() -> None:
    assert input_message(HumanMessage(content="What's the weather?")) == {
        "role": "user",
        "parts": [{"type": "text", "content": "What's the weather?"}],
    }


def test_when_message_is_a_system_message_then_role_is_system() -> None:
    assert input_message(SystemMessage(content="Be terse."))["role"] == "system"


def test_when_message_is_an_ai_message_then_role_is_assistant() -> None:
    assert input_message(AIMessage(content="Sunny."))["role"] == "assistant"


def test_when_ai_message_calls_a_tool_then_a_tool_call_part_is_emitted() -> None:
    """The assistant's tool-call message is what a decision row points back
    to, so the call id and the arguments have to survive the conversion."""
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_weather",
                "args": {"city": "Paris"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )

    assert input_message(message) == {
        "role": "assistant",
        # content "" contributes no part: a tool-calling turn normally has
        # no text, and an empty text part would be noise on every such row.
        "parts": [
            {
                "type": "tool_call",
                "id": "call_1",
                "name": "get_weather",
                "arguments": {"city": "Paris"},
            }
        ],
    }


def test_when_ai_message_has_text_and_a_tool_call_then_both_parts_survive() -> None:
    message = AIMessage(
        content="Let me look that up.",
        tool_calls=[
            {"name": "get_weather", "args": {}, "id": "call_1", "type": "tool_call"}
        ],
    )

    parts = input_message(message)["parts"]

    assert [part["type"] for part in parts] == ["text", "tool_call"]


def test_when_a_tool_call_failed_to_parse_then_it_is_kept_with_its_error() -> None:
    """A model that asked for a tool in malformed JSON is exactly what a
    reader is trying to explain, so the raw string and the parse error are
    both kept rather than dropped with the call."""
    message = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "get_weather",
                "args": "{'city': ",
                "id": "call_1",
                "error": "Malformed args.",
                "type": "invalid_tool_call",
            }
        ],
    )

    [part] = input_message(message)["parts"]
    assert part["arguments"] == "{'city': "
    assert part["error"] == "Malformed args."


def test_when_message_is_a_tool_result_then_it_becomes_a_tool_call_response() -> None:
    """The only place a tool's return value is stored: a policy_decision row
    records the call, never what came back."""
    message = ToolMessage(
        content="sunny, 21C", tool_call_id="call_1", name="get_weather"
    )

    assert input_message(message) == {
        "role": "tool",
        "parts": [
            {"type": "tool_call_response", "id": "call_1", "response": "sunny, 21C"}
        ],
    }


def test_when_content_is_a_block_list_then_text_blocks_are_flattened() -> None:
    """Multimodal content: the text collapses into GenAI text parts, and the
    image block is carried through whole — dropping it would lose exactly the
    attachment an investigator came for."""
    image = {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
    message = HumanMessage(content=[{"type": "text", "text": "Describe this"}, image])

    assert input_message(message)["parts"] == [
        {"type": "text", "content": "Describe this"},
        image,
    ]


def test_when_a_content_block_is_a_bare_string_then_it_becomes_a_text_part() -> None:
    """``content`` may be a list of plain strings, not only of typed blocks."""
    assert input_message(HumanMessage(content=["first", "second"]))["parts"] == [
        {"type": "text", "content": "first"},
        {"type": "text", "content": "second"},
    ]


def test_when_message_is_a_bare_string_then_it_is_a_user_text_message() -> None:
    """The non-chat ``on_llm_start`` path hands over prompt strings."""
    assert input_message("Say hi") == {
        "role": "user",
        "parts": [{"type": "text", "content": "Say hi"}],
    }


def test_when_the_message_is_a_streamed_chunk_then_the_role_still_resolves() -> None:
    """A streamed call hands ``on_llm_end`` a merged chunk, not a plain
    message, and a chunk's ``type`` is its own class name — so an unnormalised
    lookup would file the model's answer under ``user``."""
    assert input_message(AIMessageChunk(content="Sunny."))["role"] == "assistant"
    assert input_message(ToolMessageChunk(content="21C", tool_call_id="call_1")) == {
        "role": "tool",
        "parts": [{"type": "tool_call_response", "id": "call_1", "response": "21C"}],
    }


def test_when_message_is_a_chat_message_then_its_own_role_is_used() -> None:
    assert input_message(ChatMessage(content="hi", role="critic"))["role"] == "critic"


# --- output_messages ----------------------------------------------------------


def test_output_messages_happy_path() -> None:
    result = LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="Sunny."))]]
    )

    assert output_messages(result) == [
        {"role": "assistant", "parts": [text_part("Sunny.")]}
    ]


def test_when_the_completion_is_a_tool_call_then_it_is_a_tool_call_part() -> None:
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_weather",
                "args": {"city": "Paris"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    result = LLMResult(generations=[[ChatGeneration(message=message)]])

    [completion] = output_messages(result)
    assert completion["parts"][0]["name"] == "get_weather"


def test_when_the_llm_is_not_a_chat_model_then_the_generated_text_is_used() -> None:
    """A bare ``Generation`` has no ``.message``; the text is the whole
    completion."""
    result = LLMResult(generations=[[Generation(text="Sunny.")]])

    assert output_messages(result) == [
        {"role": "assistant", "parts": [text_part("Sunny.")]}
    ]


def test_when_the_completion_was_streamed_then_it_is_still_the_assistant() -> None:
    """``BaseChatModel.astream`` merges the chunks and hands ``on_llm_end`` a
    ``ChatGenerationChunk`` carrying an ``AIMessageChunk``."""
    result = LLMResult(
        generations=[[ChatGeneration(message=AIMessageChunk(content="Sunny."))]]
    )

    assert output_messages(result) == [
        {"role": "assistant", "parts": [text_part("Sunny.")]}
    ]


def test_when_there_are_no_generations_then_there_is_no_completion() -> None:
    assert output_messages(LLMResult(generations=[])) == []


# --- split_system_instructions ------------------------------------------------


def test_split_system_instructions_happy_path() -> None:
    """LangChain keeps the system prompt at the head of the list; the wire
    contract carries it in its own field, so it is lifted rather than copied."""
    messages = [
        {"role": "system", "parts": [text_part("Be terse.")]},
        {"role": "user", "parts": [text_part("Hi")]},
    ]

    instructions, rest = split_system_instructions(messages)

    assert instructions == [text_part("Be terse.")]
    assert rest == [{"role": "user", "parts": [text_part("Hi")]}]


def test_when_there_is_no_system_message_then_nothing_is_lifted() -> None:
    messages = [{"role": "user", "parts": [text_part("Hi")]}]

    assert split_system_instructions(messages) == ([], messages)


def test_when_a_system_message_is_mid_conversation_then_it_stays_put() -> None:
    """Only the leading run is instructions. One injected later is part of the
    exchange and belongs in the transcript where it happened."""
    messages = [
        {"role": "user", "parts": [text_part("Hi")]},
        {"role": "system", "parts": [text_part("Now be terse.")]},
    ]

    assert split_system_instructions(messages) == ([], messages)
