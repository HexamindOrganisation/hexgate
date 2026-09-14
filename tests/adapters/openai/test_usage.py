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
from hexgate.adapters.openai.usage import HexgateUsageHooks
from hexgate.runtime import run_scope
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
    """Capture emit_llm_messages() calls. Autouse so no test that drives the
    hook can reach the real emit, which would build an OTLP sender for the
    fake api_key."""
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

    real = usage_mod.output_messages
    calls = {"n": 0}

    def boom(output: list[Any]) -> list[Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("conversion exploded")
        return real(output)

    monkeypatch.setattr(usage_mod, "output_messages", boom)
    hooks = HexgateUsageHooks(api_key="k")
    agent = Agent(name="my-agent", model="gpt-4o")
    context = object()

    await hooks.on_llm_start(
        context=context, agent=agent, system_prompt=None, input_items=[_user("x")]
    )
    await hooks.on_llm_end(context=context, agent=agent, response=_response())

    assert messages == []
    assert len(emitted) == 1

    # The failed call must not have spent the turn's seq: the cursor is asked
    # only once the conversion succeeded, so the next event is still seq 0 and
    # a reader sees no hole.
    await hooks.on_llm_start(
        context=context, agent=agent, system_prompt=None, input_items=[_user("x")]
    )
    await hooks.on_llm_end(context=context, agent=agent, response=_response())

    [call] = messages
    assert call["message_seq"] == 0


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


@pytest.mark.asyncio
async def test_when_two_runs_share_a_process_then_turn_keys_differ(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    """The turn key is the run id, not the address of the run context: that
    object is freed at run end and CPython hands the next run the same
    address, which would file two unrelated conversations under one key, each
    restarting message_seq at 0 — silently, since the rows still insert."""
    agent = Agent(name="my-agent", model="gpt-4o")

    for _ in range(2):
        hooks = HexgateUsageHooks(api_key="k")
        with run_scope(agent.name):
            await hooks.on_llm_start(
                context=object(),
                agent=agent,
                system_prompt=None,
                input_items=[_user("x")],
            )
            await hooks.on_llm_end(context=object(), agent=agent, response=_response())

    assert [c["message_seq"] for c in messages] == [0, 0]
    assert messages[0]["turn_key"] != messages[1]["turn_key"]


@pytest.mark.asyncio
async def test_when_there_is_no_run_scope_then_the_turn_key_is_still_unique(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    """``HexgateRunner`` always opens a run scope; a bare ``Runner.run`` handed
    these hooks does not, and the per-instance fallback keeps the key unique to
    one hooks object rather than collapsing every such run onto ``":agent"``."""
    agent = Agent(name="my-agent", model="gpt-4o")

    for _ in range(2):
        hooks = HexgateUsageHooks(api_key="k")
        await hooks.on_llm_end(context=object(), agent=agent, response=_response())

    assert messages[0]["turn_key"] != messages[1]["turn_key"]
