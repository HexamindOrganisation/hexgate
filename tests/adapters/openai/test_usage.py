"""Tests for HexgateUsageHooks: usage extraction from ModelResponse, and the
``on_llm_start`` → ``on_llm_end`` pair that turns a Responses-API prompt and
completion into one message event."""

from __future__ import annotations

from typing import Any

import pytest
from agents import Agent
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from hexgate.adapters.openai import usage as usage_mod
from hexgate.adapters.openai.usage import (
    HexgateUsageHooks,
    _input_message,
    _output_messages,
)
from hexgate.tracing.messages import LOG_MESSAGES_ENV


class _StubModel(Model):
    """Minimal concrete Model for testing agent.model resolution."""

    async def get_response(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError

    def stream_response(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError


class _ModelWithId(_StubModel):
    def __init__(self, model_id: str) -> None:
        self.model = model_id


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


def _response(
    input_tokens: int = 10,
    output_tokens: int = 20,
    output: list[Any] | None = None,
) -> ModelResponse:
    return ModelResponse(
        output=[_text_output()] if output is None else output,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        response_id=None,
    )


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


@pytest.fixture()
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture emit_llm_usage() calls without touching the sender registry."""
    calls: list[dict[str, Any]] = []

    def fake_emit(
        agent_name: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        api_key: str,
    ) -> None:
        calls.append(
            dict(
                agent_name=agent_name,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                api_key=api_key,
            )
        )

    monkeypatch.setattr(usage_mod, "emit_llm_usage", fake_emit)
    return calls


@pytest.fixture(autouse=True)
def messages(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture emit_llm_messages() calls. Autouse: every test in this module
    drives the real hook, and an unpatched emit would build an OTLP sender for
    the fake api_key."""
    calls: list[dict[str, Any]] = []

    def fake_emit(
        agent_name: str,
        model: str,
        input_messages: list[Any],
        output_messages: list[Any],
        *,
        turn_key: str,
        message_seq: int,
        system_instructions: list[Any] | None = None,
        resynced: bool = False,
        api_key: str | None = None,
    ) -> None:
        calls.append(
            dict(
                agent_name=agent_name,
                model=model,
                input_messages=input_messages,
                output_messages=output_messages,
                turn_key=turn_key,
                message_seq=message_seq,
                system_instructions=system_instructions,
                resynced=resynced,
                api_key=api_key,
            )
        )

    monkeypatch.setattr(usage_mod, "emit_llm_messages", fake_emit)
    return calls


# --- Usage -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_llm_end_emits_usage_from_response(
    emitted: list[dict[str, Any]],
) -> None:
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model="gpt-4o")

    await hooks.on_llm_end(context=object(), agent=agent, response=_response(10, 20))

    [call] = emitted
    assert call == {
        "agent_name": "my-agent",
        "model": "gpt-4o",
        "input_tokens": 10,
        "output_tokens": 20,
        "api_key": "k",
    }


@pytest.mark.asyncio
async def test_on_llm_end_when_model_is_none_then_model_is_default(
    emitted: list[dict[str, Any]],
) -> None:
    """agent.model defaults to None when unset -- the agent uses whatever
    model the runner/SDK resolves at call time, which this hook never sees.
    "default" is an honest placeholder rather than a guess. Not "" -- the
    platform rejects an empty `model` outright (min_length=1), which would
    silently drop the event."""
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent")  # model defaults to None

    await hooks.on_llm_end(context=object(), agent=agent, response=_response())

    [call] = emitted
    assert call["model"] == "default"


@pytest.mark.asyncio
async def test_on_llm_end_when_model_is_a_model_instance_then_model_is_its_id(
    emitted: list[dict[str, Any]],
) -> None:
    """Standard Model impls (e.g. OpenAIResponsesModel) expose the real
    model id via .model -- that should be reported, not the class name."""
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model=_ModelWithId("gpt-4o"))

    await hooks.on_llm_end(context=object(), agent=agent, response=_response())

    [call] = emitted
    assert call["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_on_llm_end_when_model_is_an_exotic_model_then_model_is_class_name(
    emitted: list[dict[str, Any]],
) -> None:
    """A Model implementation with no .model attribute has no guaranteed
    name field (agents.models.interface.Model exposes none), so it falls
    back to the instance's class name."""
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model=_StubModel())

    await hooks.on_llm_end(context=object(), agent=agent, response=_response())

    [call] = emitted
    assert call["model"] == "_StubModel"


# --- Input-item conversion ----------------------------------------------------


def test_input_message_happy_path() -> None:
    assert _input_message({"role": "user", "content": "Hi"}) == {
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

    assert _input_message(item) == {
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

    assert _input_message(item) == {
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

    assert _input_message(item) == {
        "role": "tool",
        "parts": [
            {"type": "tool_call_response", "id": "call_1", "response": "sunny, 22C"}
        ],
    }


def test_when_item_has_no_role_or_known_type_then_it_is_carried_through() -> None:
    """Reasoning items and built-in tool calls have no GenAI part to map onto;
    keeping them whole leaves the turn complete instead of holed."""
    item = {"type": "reasoning", "id": "rs_1", "summary": []}

    assert _input_message(item) == {"role": "assistant", "parts": [item]}


# --- Output conversion --------------------------------------------------------


def test_output_messages_happy_path() -> None:
    assert _output_messages([_text_output("Sunny, 24C.")]) == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Sunny, 24C."}]}
    ]


def test_when_response_has_text_and_a_tool_call_then_they_share_one_message() -> None:
    """One model call is one completion; the Responses API just splits it
    across items."""
    assert _output_messages([_text_output("Checking."), _tool_call_output()]) == [
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


def test_when_response_output_is_empty_then_there_are_no_output_messages() -> None:
    assert _output_messages([]) == []


# --- The hook pair ------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_llm_end_emits_messages_happy_path(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model="gpt-4o")
    context = object()

    await hooks.on_llm_start(
        context=context,
        agent=agent,
        system_prompt="Be brief.",
        input_items=[_user("Weather in Paris?")],
    )
    await hooks.on_llm_end(context=context, agent=agent, response=_response())

    [call] = messages
    assert call["agent_name"] == "my-agent"
    assert call["model"] == "gpt-4o"
    assert call["api_key"] == "k"
    assert call["message_seq"] == 0
    assert call["resynced"] is False
    assert call["input_messages"] == [
        {"role": "user", "parts": [{"type": "text", "content": "Weather in Paris?"}]}
    ]
    assert call["output_messages"] == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Sunny, 24C."}]}
    ]
    assert call["system_instructions"] == [{"type": "text", "content": "Be brief."}]


@pytest.mark.asyncio
async def test_when_turn_continues_then_only_the_new_items_are_emitted(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    """The second call's input list is the whole conversation again; only the
    tool-call message and the tool result are new. System instructions ride on
    the first event of the turn only."""
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model="gpt-4o")
    context = object()
    first = [_user("Weather in Paris?")]
    second = first + [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city": "Paris"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "sunny, 22C"},
    ]

    await hooks.on_llm_start(
        context=context, agent=agent, system_prompt="Be brief.", input_items=first
    )
    await hooks.on_llm_end(
        context=context, agent=agent, response=_response(output=[_tool_call_output()])
    )
    await hooks.on_llm_start(
        context=context, agent=agent, system_prompt="Be brief.", input_items=second
    )
    await hooks.on_llm_end(context=context, agent=agent, response=_response())

    assert [c["message_seq"] for c in messages] == [0, 1]
    assert messages[0]["turn_key"] == messages[1]["turn_key"]
    assert [m["role"] for m in messages[1]["input_messages"]] == ["assistant", "tool"]
    assert messages[1]["system_instructions"] is None
    assert messages[1]["resynced"] is False


@pytest.mark.asyncio
async def test_when_the_list_was_rewritten_then_the_event_is_resynced(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    """A framework trimming the history to fit the context window leaves the
    mark meaningless, so the whole list goes out flagged instead of a wrong
    slice."""
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model="gpt-4o")
    context = object()

    await hooks.on_llm_start(
        context=context,
        agent=agent,
        system_prompt=None,
        input_items=[_user("one"), _user("two"), _user("three")],
    )
    await hooks.on_llm_end(context=context, agent=agent, response=_response())
    await hooks.on_llm_start(
        context=context,
        agent=agent,
        system_prompt=None,
        input_items=[_user("summary of one and two"), _user("three")],
    )
    await hooks.on_llm_end(context=context, agent=agent, response=_response())

    assert messages[1]["resynced"] is True
    assert messages[1]["message_seq"] == 1
    assert len(messages[1]["input_messages"]) == 2


@pytest.mark.asyncio
async def test_when_a_handoff_switches_agent_then_each_keeps_its_own_turn(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    """A handoff keeps the run context but starts a fresh message list. Keyed
    on the context alone, the target's first call would look like a jump and
    resync; keyed per list, it is an ordinary first event at seq 0."""
    hooks = HexgateUsageHooks(api_key="k")
    context = object()
    triage = Agent(name="triage", model="gpt-4o")
    specialist = Agent(name="specialist", model="gpt-4o")

    await hooks.on_llm_start(
        context=context, agent=triage, system_prompt="Triage.", input_items=[_user("a")]
    )
    await hooks.on_llm_end(context=context, agent=triage, response=_response())
    await hooks.on_llm_start(
        context=context,
        agent=specialist,
        system_prompt="Specialise.",
        input_items=[_user("a"), _user("b")],
    )
    await hooks.on_llm_end(context=context, agent=specialist, response=_response())

    assert messages[0]["turn_key"] != messages[1]["turn_key"]
    assert [c["message_seq"] for c in messages] == [0, 0]
    assert messages[1]["resynced"] is False
    assert messages[1]["system_instructions"] == [
        {"type": "text", "content": "Specialise."}
    ]


@pytest.mark.asyncio
async def test_when_log_messages_is_off_then_usage_still_emits(
    emitted: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-out switches off message content only; token usage and
    decisions keep flowing."""
    monkeypatch.setenv(LOG_MESSAGES_ENV, "0")
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model="gpt-4o")
    context = object()

    await hooks.on_llm_start(
        context=context,
        agent=agent,
        system_prompt="Be brief.",
        input_items=[_user("x")],
    )
    await hooks.on_llm_end(context=context, agent=agent, response=_response())

    assert messages == []
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_when_conversion_raises_then_the_run_is_not_broken(
    emitted: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook runs inside the SDK's run loop, which re-raises. Losing a
    transcript row must not fail the run it was logging."""

    def boom(_output: list[Any]) -> list[Any]:
        raise RuntimeError("conversion exploded")

    monkeypatch.setattr(usage_mod, "_output_messages", boom)
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model="gpt-4o")
    context = object()

    await hooks.on_llm_start(
        context=context, agent=agent, system_prompt=None, input_items=[_user("x")]
    )
    await hooks.on_llm_end(context=context, agent=agent, response=_response())

    assert messages == []
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_when_the_prompt_was_not_stashed_then_the_completion_still_lands(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    """Every delta asked for must be emitted, empty input included — the seq
    is spent either way, and a reader is specified to read a hole as a lost
    row."""
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model="gpt-4o")

    await hooks.on_llm_end(context=object(), agent=agent, response=_response())

    [call] = messages
    assert call["input_messages"] == []
    assert call["message_seq"] == 0
    assert call["output_messages"] == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Sunny, 24C."}]}
    ]
