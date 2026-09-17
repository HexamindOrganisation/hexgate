"""pydantic_ai ``ModelMessage``s → OTel GenAI messages: the pure conversion
layer in ``hexgate.adapters.pydantic_ai.messages``. No emission — what goes in
is a run's messages, what comes out lands in the ``llm_message``
content columns."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    BuiltinToolCallPart,
    BuiltinToolReturnPart,
    CompactionPart,
    ImageUrl,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextContent,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from hexgate.adapters.pydantic_ai.messages import run_messages


def _user(prompt: Any = "hello", **kwargs: Any) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(prompt)], **kwargs)


def _answer(text: str = "hi") -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def test_run_messages_happy_path() -> None:
    """The last response is the completion; everything before it the input."""
    history = [_user(), _answer()]

    input_messages, output, system = run_messages(history)

    assert input_messages == [
        {"role": "user", "parts": [{"type": "text", "content": "hello"}]}
    ]
    assert output == [
        {"role": "assistant", "parts": [{"type": "text", "content": "hi"}]}
    ]
    assert system is None


def test_when_a_tool_is_called_then_the_call_and_its_return_are_both_kept() -> None:
    """A decision event records the call but never its return value, so the
    transcript is the only place the return lands."""
    history = [
        _user(),
        ModelResponse(parts=[ToolCallPart("get_weather", {"city": "Paris"}, "c1")]),
        ModelRequest(parts=[ToolReturnPart("get_weather", "24C", "c1")]),
        _answer(),
    ]

    input_messages, _, _ = run_messages(history)

    assert input_messages[1] == {
        "role": "assistant",
        "parts": [
            {
                "type": "tool_call",
                "id": "c1",
                "name": "get_weather",
                "arguments": {"city": "Paris"},
            }
        ],
    }
    assert input_messages[2] == {
        "role": "tool",
        "parts": [
            {
                "type": "tool_call_response",
                "id": "c1",
                "name": "get_weather",
                "response": "24C",
            }
        ],
    }


def test_when_tool_calls_run_in_parallel_then_their_returns_are_one_message() -> None:
    history = [
        ModelRequest(
            parts=[
                ToolReturnPart("a", "1", "c1"),
                ToolReturnPart("b", "2", "c2"),
            ]
        ),
        _answer(),
    ]

    input_messages, _, _ = run_messages(history)

    [message] = input_messages
    assert message["role"] == "tool"
    assert [part["id"] for part in message["parts"]] == ["c1", "c2"]


def test_when_the_completion_carries_thinking_then_it_is_dropped() -> None:
    """Every part of the completion shares one 8 KiB budget, so reasoning
    starves the answer beside it (issue #221)."""
    history = [_user(), ModelResponse(parts=[ThinkingPart("hmm"), TextPart("hi")])]

    _, output, _ = run_messages(history)

    assert output == [
        {"role": "assistant", "parts": [{"type": "text", "content": "hi"}]}
    ]


def test_when_an_earlier_turn_carries_thinking_then_the_input_drops_it_too() -> None:
    """Unlike the per-call adapters, the input here is the whole run, so the
    reasoning of every past response would ride in it."""
    history = [
        _user(),
        ModelResponse(parts=[ThinkingPart("hmm"), TextPart("first")]),
        _user("again"),
        _answer(),
    ]

    input_messages, _, _ = run_messages(history)

    assert input_messages[1] == {
        "role": "assistant",
        "parts": [{"type": "text", "content": "first"}],
    }


def test_when_the_completion_is_only_thinking_then_no_message_is_emitted() -> None:
    history = [_user(), ModelResponse(parts=[ThinkingPart("hmm")])]

    _, output, _ = run_messages(history)

    assert output == []


def test_when_a_request_carries_instructions_then_they_become_system_instructions() -> (
    None
):
    """Lifted out of the messages: the wire contract carries them in their own
    field, under their own cap."""
    history = [
        ModelRequest(
            parts=[SystemPromptPart("You are a bot."), UserPromptPart("hello")],
            instructions="Be terse.",
        ),
        _answer(),
    ]

    input_messages, _, system = run_messages(history)

    assert system == [
        {"type": "text", "content": "Be terse."},
        {"type": "text", "content": "You are a bot."},
    ]
    assert input_messages == [
        {"role": "user", "parts": [{"type": "text", "content": "hello"}]}
    ]


def test_when_instructions_are_re_rendered_then_the_last_is_kept() -> None:
    """pydantic_ai re-renders them on every request, so a dynamic
    `@agent.instructions` returns a different string each step — and the last
    is the one the completion was produced under."""
    history = [
        _user(instructions="step 1"),
        _answer("first"),
        _user("again", instructions="step 2"),
        _answer(),
    ]

    _, _, system = run_messages(history)

    assert system == [{"type": "text", "content": "step 2"}]


def test_when_the_output_came_back_through_a_tool_then_the_completion_is_still_found() -> (
    None
):
    """An `output_type=` agent — pydantic_ai's default for structured output —
    ends its history on the ModelRequest carrying the `final_result` tool
    return, so the last message is not the completion."""
    call = ToolCallPart("final_result", {"summary": "down"}, "c1")
    history = [
        _user(),
        ModelResponse(parts=[call]),
        ModelRequest(parts=[ToolReturnPart("final_result", "processed", "c1")]),
    ]

    input_messages, output, _ = run_messages(history)

    assert output == [
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "id": "c1",
                    "name": "final_result",
                    "arguments": {"summary": "down"},
                }
            ],
        }
    ]
    assert [m["role"] for m in input_messages] == ["user", "tool"]


def test_when_the_run_holds_no_response_then_there_is_no_completion() -> None:
    """A stream the caller aborted before the first token."""
    history = [ModelRequest(parts=[UserPromptPart("hello")])]

    input_messages, output, _ = run_messages(history)

    assert output == []
    assert len(input_messages) == 1


def test_run_messages_when_the_history_is_empty_then_nothing_is_emitted() -> None:
    assert run_messages([]) == ([], [], None)


def test_when_a_retry_names_a_tool_then_it_is_a_failed_tool_response() -> None:
    history = [
        ModelRequest(
            parts=[RetryPromptPart("bad args", tool_name="a", tool_call_id="c1")]
        ),
        _answer(),
    ]

    input_messages, _, _ = run_messages(history)

    assert input_messages == [
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_call_response",
                    "id": "c1",
                    "name": "a",
                    "response": "bad args",
                    "error": True,
                }
            ],
        }
    ]


def test_when_a_retry_names_no_tool_then_it_is_feedback_on_the_model_text() -> None:
    history = [ModelRequest(parts=[RetryPromptPart("not JSON")]), _answer()]

    input_messages, _, _ = run_messages(history)

    assert input_messages[0]["role"] == "user"


def test_when_a_builtin_tool_runs_then_its_call_and_return_convert_alike() -> None:
    """Provider-side tools use their own part kinds but the same shapes."""
    history = [
        ModelResponse(
            parts=[
                BuiltinToolCallPart("web_search", {"q": "x"}, "c1"),
                BuiltinToolReturnPart("web_search", "results", "c1"),
            ]
        ),
        _answer(),
    ]

    input_messages, _, _ = run_messages(history)

    assert [message["role"] for message in input_messages] == ["assistant", "tool"]
    assert input_messages[0]["parts"][0]["type"] == "tool_call"
    assert input_messages[1]["parts"][0]["type"] == "tool_call_response"


def test_when_a_user_prompt_is_multimodal_then_each_item_keeps_its_kind() -> None:
    history = [
        _user(["look", TextContent("tagged"), ImageUrl("https://x/y.png")]),
        _answer(),
    ]

    input_messages, _, _ = run_messages(history)

    [message] = input_messages
    assert [part["type"] for part in message["parts"]] == ["text", "text", "image-url"]


def test_when_content_is_inline_bytes_then_only_a_descriptor_is_kept() -> None:
    """Carried through whole, bytes serialise to an escaped repr four times
    their size — one inlined image fills the 256 KiB input budget with
    something nobody can read, capping away every message beside it."""
    image = BinaryContent(b"\x89PNG" + b"\x00" * 4096, media_type="image/png")
    history = [_user(["look", image]), _answer()]

    input_messages, _, _ = run_messages(history)

    assert input_messages[0]["parts"][1] == {
        "type": "binary",
        "media_type": "image/png",
        "bytes": 4100,
    }


def test_when_a_part_has_no_genai_equivalent_then_it_is_carried_through_whole() -> None:
    """The transcript exists to explain a run after the fact, so an unmapped
    part is kept rather than dropped."""
    part = CompactionPart(content="summary")
    history = [ModelResponse(parts=[part]), _answer()]

    input_messages, _, _ = run_messages(history)

    assert input_messages == [
        {"role": "assistant", "parts": [{"type": "compaction", "part": part}]}
    ]


@pytest.mark.parametrize("args", ['{"city": "Paris"}', {"city": "Paris"}, None])
def test_when_tool_arguments_arrive_in_any_form_then_they_are_not_re_encoded(
    args: Any,
) -> None:
    """Parsing would change the record, and models do emit malformed JSON."""
    history = [ModelResponse(parts=[ToolCallPart("a", args, "c1")]), _answer()]

    input_messages, _, _ = run_messages(history)

    assert input_messages[0]["parts"][0]["arguments"] == args
