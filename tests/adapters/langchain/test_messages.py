"""Tests for the LangChain ``BaseMessage`` → GenAI ``role``/``parts``
converters. Pure functions over plain dicts — no handler, no emit."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    FunctionMessage,
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


def _tool_call(call_id: str | None = "call_1") -> dict[str, Any]:
    return {
        "name": "get_weather",
        "args": {"city": "Paris"},
        "id": call_id,
        "type": "tool_call",
    }


# --- Roles --------------------------------------------------------------------


def test_input_message_happy_path() -> None:
    assert input_message(HumanMessage(content="What's the weather?")) == {
        "role": "user",
        "parts": [{"type": "text", "content": "What's the weather?"}],
    }


@pytest.mark.parametrize(
    ("message", "role"),
    [
        (SystemMessage(content="Be terse."), "system"),
        (HumanMessage(content="Hi"), "user"),
        (AIMessage(content="Sunny."), "assistant"),
        # A chunk's ``type`` is its own class name ("AIMessageChunk"), so an
        # unnormalised lookup files a streamed completion under "user" — the
        # model's own answer attributed to the user.
        (AIMessageChunk(content="Sunny."), "assistant"),
        (ToolMessageChunk(content="21C", tool_call_id="call_1"), "tool"),
        # ChatMessage carries a caller-chosen role.
        (ChatMessage(content="hi", role="critic"), "critic"),
        # A bare prompt string from the non-chat ``on_llm_start`` path.
        ("Say hi", "user"),
    ],
)
def test_when_the_message_is_X_then_the_role_is_Y(message: Any, role: str) -> None:
    assert input_message(message)["role"] == role


# --- Tool calls and their results ---------------------------------------------


def test_when_ai_message_calls_a_tool_then_a_tool_call_part_is_emitted() -> None:
    """Empty ``content`` contributes no part: a tool-calling turn normally has
    no text, and an empty text part would be noise on every such row."""
    message = AIMessage(content="", tool_calls=[_tool_call()])

    assert input_message(message) == {
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


def test_when_ai_message_has_text_and_a_tool_call_then_both_parts_survive() -> None:
    message = AIMessage(content="Looking that up.", tool_calls=[_tool_call()])

    assert [part["type"] for part in input_message(message)["parts"]] == [
        "text",
        "tool_call",
    ]


def test_when_a_tool_call_failed_to_parse_then_it_is_kept_with_its_error() -> None:
    """A model that asked for a tool in malformed JSON is exactly what a reader
    is trying to explain, so the raw string and the error both survive."""
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


@pytest.mark.parametrize(
    ("message", "kinds"),
    [
        # Promoted into the standardized field, so the block would be a second
        # copy of one call under a different id (the block carries the
        # output-item id, the part the call id a ToolMessage correlates on).
        pytest.param(
            AIMessage(
                content=[{"type": "function_call", "call_id": "call_1", "id": "fc_1"}],
                tool_calls=[_tool_call()],
            ),
            ["tool_call"],
            id="openai-responses-promoted",
        ),
        pytest.param(
            AIMessage(
                content=[{"type": "tool_use", "id": "call_1", "name": "get_weather"}],
                tool_calls=[_tool_call()],
            ),
            ["tool_call"],
            id="anthropic-promoted",
        ),
        # Nothing promoted it, so the block is the only record there is — an
        # un-translated provider shape, a hand-built message, a replayed
        # transcript. Dropping by block *type* would lose the call entirely.
        pytest.param(
            AIMessage(
                content=[{"type": "function_call", "call_id": "call_1", "id": "fc_1"}]
            ),
            ["function_call"],
            id="orphan-block",
        ),
        # Per call id, not per message: a partially translated message keeps
        # the half with no standardized counterpart.
        pytest.param(
            AIMessage(
                content=[
                    {"type": "tool_use", "id": "call_1"},
                    {"type": "tool_use", "id": "call_2"},
                ],
                tool_calls=[_tool_call()],
            ),
            ["tool_use", "tool_call"],
            id="one-of-two-promoted",
        ),
        # ToolCall.id is str | None. Matching on a truthy id would leave an
        # id-less block matching nothing and going out beside the part built
        # from the very same call — two tool_call parts, nothing to tell them
        # apart, which is the double-record the rule exists to prevent.
        pytest.param(
            AIMessage(
                content=[{"type": "tool_use", "name": "get_weather"}],
                tool_calls=[_tool_call(None)],
            ),
            ["tool_call"],
            id="both-id-less",
        ),
        # The provider ran this one itself, so it never reaches tool_calls and
        # the block is the only record of it.
        pytest.param(
            AIMessage(content=[{"type": "server_tool_use", "id": "s1"}]),
            ["server_tool_use"],
            id="provider-run-tool",
        ),
    ],
)
def test_a_tool_call_block_is_dropped_only_when_the_standardized_field_repeats_it(
    message: BaseMessage, kinds: list[str]
) -> None:
    assert [part["type"] for part in input_message(message)["parts"]] == kinds


@pytest.mark.parametrize(
    ("message", "call_id", "name"),
    [
        (
            ToolMessage(content="21C", tool_call_id="call_1", name="get_weather"),
            "call_1",
            "get_weather",
        ),
        # FunctionMessage has no tool_call_id field at all, so ``name`` is the
        # only thing saying which call this answers.
        (FunctionMessage(content="21C", name="get_weather"), "", "get_weather"),
    ],
)
def test_a_tool_result_becomes_a_tool_call_response(
    message: BaseMessage, call_id: str, name: str
) -> None:
    """The only place a tool's return value is stored: a policy_decision row
    records the call, never what came back."""
    assert input_message(message) == {
        "role": "tool",
        "parts": [
            {
                "type": "tool_call_response",
                "id": call_id,
                "name": name,
                "response": "21C",
            }
        ],
    }


# --- Content blocks -----------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "parts"),
    [
        # Multimodal: the text collapses into a GenAI text part and the image
        # is carried through whole — dropping it would lose exactly the
        # attachment an investigator came for.
        (
            [{"type": "text", "text": "Describe this"}, {"type": "image_url"}],
            [{"type": "text", "content": "Describe this"}, {"type": "image_url"}],
        ),
        # A list of plain strings, not typed blocks.
        (["first"], [{"type": "text", "content": "first"}]),
    ],
)
def test_when_content_is_a_block_list_then_text_blocks_are_flattened(
    content: Any, parts: list[dict[str, Any]]
) -> None:
    assert input_message(HumanMessage(content=content))["parts"] == parts


# --- output_messages ----------------------------------------------------------


@pytest.mark.parametrize(
    ("generations", "expected"),
    [
        (
            [ChatGeneration(message=AIMessage(content="Sunny."))],
            [{"role": "assistant", "parts": [text_part("Sunny.")]}],
        ),
        # Streaming merges the chunks and hands on_llm_end a ChatGenerationChunk.
        (
            [ChatGeneration(message=AIMessageChunk(content="Sunny."))],
            [{"role": "assistant", "parts": [text_part("Sunny.")]}],
        ),
        # A non-chat LLM yields a bare Generation with no .message.
        (
            [Generation(text="Sunny.")],
            [{"role": "assistant", "parts": [text_part("Sunny.")]}],
        ),
        ([], []),
    ],
)
def test_output_messages_happy_path(
    generations: list[Any], expected: list[dict[str, Any]]
) -> None:
    result = LLMResult(generations=[generations] if generations else [])

    assert output_messages(result) == expected


def test_when_the_completion_is_a_tool_call_then_it_is_a_tool_call_part() -> None:
    result = LLMResult(
        generations=[
            [ChatGeneration(message=AIMessage(content="", tool_calls=[_tool_call()]))]
        ]
    )

    [completion] = output_messages(result)
    assert completion["parts"][0]["name"] == "get_weather"


# --- split_system_instructions ------------------------------------------------


def test_split_system_instructions_happy_path() -> None:
    """LangChain keeps the system prompt at the head of the list; the wire
    contract carries it in its own field, so it is lifted rather than copied."""
    user = {"role": "user", "parts": [text_part("Hi")]}
    messages = [{"role": "system", "parts": [text_part("Be terse.")]}, user]

    assert split_system_instructions(messages) == ([text_part("Be terse.")], [user])


@pytest.mark.parametrize(
    "messages",
    [
        pytest.param([{"role": "user", "parts": []}], id="no-system-message"),
        # Only the leading run is instructions. One injected later is part of
        # the exchange and belongs where it happened.
        pytest.param(
            [{"role": "user", "parts": []}, {"role": "system", "parts": []}],
            id="system-message-mid-conversation",
        ),
    ],
)
def test_when_there_is_no_leading_system_message_then_nothing_is_lifted(
    messages: list[dict[str, Any]],
) -> None:
    assert split_system_instructions(messages) == ([], messages)
