"""Tests for HexgateUsageCallbackHandler: usage extraction from LLMResult, and
the ``on_chat_model_start`` → ``on_llm_end`` pair that turns a LangChain
message list and its completion into one message event."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, Generation, LLMResult

from hexgate.adapters.langchain import usage as usage_mod
from hexgate.adapters.langchain.usage import HexgateUsageCallbackHandler
from hexgate.runtime import run_scope
from hexgate.tracing.messages import LOG_MESSAGES_ENV


def _result(
    *, usage_metadata: dict | None = None, llm_output: dict | None = None
) -> LLMResult:
    message = AIMessage(content="hi", usage_metadata=usage_metadata)
    return LLMResult(
        generations=[[ChatGeneration(message=message)]],
        llm_output=llm_output or {},
    )


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
    handler can reach the real emit, which would build an OTLP sender for the
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


async def _turn(
    handler: HexgateUsageCallbackHandler,
    prompt: list[Any],
    completion: AIMessage,
    *,
    run_id: UUID | None = None,
) -> None:
    """One full LLM call through the handler: start with ``prompt``, end with
    ``completion``. A fresh ``run_id`` each time, as LangChain mints one per
    model call."""
    call_id = run_id or uuid4()
    await handler.on_chat_model_start({}, [prompt], run_id=call_id)
    await handler.on_llm_end(
        LLMResult(
            generations=[[ChatGeneration(message=completion)]],
            llm_output={"model_name": "gpt-4o"},
        ),
        run_id=call_id,
    )


@pytest.mark.asyncio
async def test_on_llm_end_emits_usage_from_standardized_metadata(
    emitted: list[dict[str, Any]],
) -> None:
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")
    response = _result(
        usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        llm_output={"model_name": "gpt-4o"},
    )

    await handler.on_llm_end(response, run_id=uuid4())

    [call] = emitted
    assert call == {
        "agent_name": "my-agent",
        "model": "gpt-4o",
        "input_tokens": 10,
        "output_tokens": 20,
        "api_key": "k",
    }


@pytest.mark.asyncio
async def test_on_llm_end_falls_back_to_legacy_token_usage(
    emitted: list[dict[str, Any]],
) -> None:
    """Providers that don't populate the standardized UsageMetadata field
    still report usage via the legacy llm_output shape."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")
    response = _result(
        usage_metadata=None,
        llm_output={
            "model_name": "legacy-model",
            "token_usage": {"prompt_tokens": 5, "completion_tokens": 7},
        },
    )

    await handler.on_llm_end(response, run_id=uuid4())

    [call] = emitted
    assert call["model"] == "legacy-model"
    assert call["input_tokens"] == 5
    assert call["output_tokens"] == 7


@pytest.mark.asyncio
async def test_on_llm_end_does_nothing_when_no_usage_reported(
    emitted: list[dict[str, Any]],
) -> None:
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")
    response = _result(usage_metadata=None, llm_output={"model_name": "gpt-4o"})

    await handler.on_llm_end(response, run_id=uuid4())

    assert emitted == []


@pytest.mark.asyncio
async def test_on_llm_end_reads_model_from_response_metadata_when_streaming(
    emitted: list[dict[str, Any]],
) -> None:
    """Streaming aggregates llm_output to None (confirmed against a real
    streaming ChatOpenAI call — LangChain's default _combine_llm_outputs,
    and ChatOpenAI's override, both skip None per-chunk outputs, which
    streaming chunks are). model_name must still come from the per-message
    response_metadata, which streaming does populate — not "" (which the
    platform's schema rejects, min_length=1)."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")
    message = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 1, "output_tokens": 8, "total_tokens": 9},
        response_metadata={"model_name": "gpt-4o-mini", "finish_reason": "stop"},
    )
    response = LLMResult(
        generations=[[ChatGeneration(message=message)]], llm_output=None
    )

    await handler.on_llm_end(response, run_id=uuid4())

    [call] = emitted
    assert call["model"] == "gpt-4o-mini"
    assert call["input_tokens"] == 1
    assert call["output_tokens"] == 8


# --- Messages ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_llm_end_emits_messages_happy_path(
    messages: list[dict[str, Any]],
) -> None:
    """The prompt stashed by ``on_chat_model_start`` and the completion seen by
    ``on_llm_end`` leave as one event, under a turn key naming the Hexgate run."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")

    with run_scope("my-agent") as facts:
        await _turn(
            handler,
            [SystemMessage(content="Be terse."), HumanMessage(content="Weather?")],
            AIMessage(content="Sunny."),
        )

    [event] = messages
    assert event["turn_key"] == f"{facts.id}:my-agent"
    assert event["message_seq"] == 0
    assert event["resynced"] is False
    assert event["model"] == "gpt-4o"
    assert event["api_key"] == "k"
    # The system prompt is lifted out of the list into its own capped field —
    # LangChain has no separate system-prompt argument.
    assert event["system_instructions"] == [{"type": "text", "content": "Be terse."}]
    assert event["input_messages"] == [
        {"role": "user", "parts": [{"type": "text", "content": "Weather?"}]}
    ]
    assert event["output_messages"] == [
        {"role": "assistant", "parts": [{"type": "text", "content": "Sunny."}]}
    ]


@pytest.mark.asyncio
async def test_when_a_second_call_extends_the_list_then_only_the_delta_is_sent(
    messages: list[dict[str, Any]],
) -> None:
    """LangGraph hands the whole conversation to every call. Re-sending it
    would store the transcript once per turn; the tool result in the delta is
    stored nowhere else, since a decision row records the call but not what it
    returned."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")
    tool_call = AIMessage(
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
    first = [SystemMessage(content="Be terse."), HumanMessage(content="Weather?")]

    with run_scope("my-agent"):
        await _turn(handler, first, tool_call)
        await _turn(
            handler,
            [
                *first,
                tool_call,
                ToolMessage(content="sunny, 21C", tool_call_id="call_1"),
            ],
            AIMessage(content="Sunny in Paris."),
        )

    _, second = messages
    assert second["message_seq"] == 1
    assert second["resynced"] is False
    # Instructions ride on the first event of the turn only.
    assert second["system_instructions"] is None
    assert [m["role"] for m in second["input_messages"]] == ["assistant", "tool"]
    assert second["input_messages"][1]["parts"][0]["response"] == "sunny, 21C"


@pytest.mark.asyncio
async def test_when_the_framework_trimmed_the_list_then_the_event_is_resynced(
    messages: list[dict[str, Any]],
) -> None:
    """Chains drop old turns to fit the context window. Slicing at a mark that
    no longer means anything would emit the wrong tail, so the whole list goes
    out flagged instead."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")

    with run_scope("my-agent"):
        await _turn(
            handler,
            [SystemMessage(content="Be terse."), HumanMessage(content="Weather?")],
            AIMessage(content="Sunny."),
        )
        await _turn(
            handler, [HumanMessage(content="And tomorrow?")], AIMessage(content="Rain.")
        )

    _, second = messages
    assert second["resynced"] is True
    assert second["message_seq"] == 1, "a resync keeps counting, it is not a new turn"
    assert [m["role"] for m in second["input_messages"]] == ["user"]


@pytest.mark.asyncio
async def test_when_two_runs_share_a_handler_then_turn_keys_differ(
    messages: list[dict[str, Any]],
) -> None:
    """One handler is built per proxy and serves every call it makes — unlike
    the OpenAI adapter's per-run hooks. Keyed on anything the two runs shared,
    run 2's first call would look like a continuation of run 1."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")

    for _ in range(2):
        with run_scope("my-agent"):
            await _turn(
                handler, [HumanMessage(content="Weather?")], AIMessage(content="Sunny.")
            )
            handler.end_run()

    first, second = messages
    assert first["turn_key"] != second["turn_key"]
    assert [event["message_seq"] for event in messages] == [0, 0]


@pytest.mark.asyncio
async def test_end_run_happy_path(
    messages: list[dict[str, Any]],
) -> None:
    """Without it, the per-turn_key state of every run the process ever made
    would accumulate for its lifetime."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")

    with run_scope("my-agent"):
        await handler.on_chat_model_start(
            {}, [[HumanMessage(content="Weather?")]], run_id=uuid4()
        )
        handler.end_run()

        assert handler._cursor._turns == {}
        assert handler._pending == {}


@pytest.mark.asyncio
async def test_when_message_logging_is_off_then_only_usage_is_emitted(
    monkeypatch: pytest.MonkeyPatch,
    emitted: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv(LOG_MESSAGES_ENV, "0")
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")

    with run_scope("my-agent"):
        await _turn(
            handler,
            [HumanMessage(content="Weather?")],
            AIMessage(
                content="Sunny.",
                usage_metadata={
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "total_tokens": 3,
                },
            ),
        )

    assert messages == []
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_when_the_prompt_was_not_stashed_then_the_completion_still_lands(
    messages: list[dict[str, Any]],
) -> None:
    """A handler attached mid-stream sees an ``on_llm_end`` whose start it
    missed. An empty input beside a real completion is a truer record than no
    row at all."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")

    with run_scope("my-agent"):
        await handler.on_llm_end(
            LLMResult(
                generations=[[ChatGeneration(message=AIMessage(content="Sunny."))]],
                llm_output={"model_name": "gpt-4o"},
            ),
            run_id=uuid4(),
        )

    [event] = messages
    assert event["input_messages"] == []
    assert event["output_messages"][0]["parts"] == [
        {"type": "text", "content": "Sunny."}
    ]


@pytest.mark.asyncio
async def test_when_the_call_failed_then_its_stash_is_dropped(
    messages: list[dict[str, Any]],
) -> None:
    """No ``on_llm_end`` follows a provider error, so the stash would sit there
    for the life of the proxy."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")
    run_id = uuid4()

    with run_scope("my-agent"):
        await handler.on_chat_model_start(
            {}, [[HumanMessage(content="Weather?")]], run_id=run_id
        )
        await handler.on_llm_error(RuntimeError("502"), run_id=run_id)

    assert handler._pending == {}
    assert messages == []


@pytest.mark.asyncio
async def test_when_converting_raises_then_the_event_is_dropped_not_the_run(
    monkeypatch: pytest.MonkeyPatch,
    emitted: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> None:
    """Losing a transcript row must not fail the run it was logging."""

    def boom(_message: Any) -> dict[str, Any]:
        raise RuntimeError("conversion exploded")

    monkeypatch.setattr(usage_mod, "input_message", boom)
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")

    with run_scope("my-agent"):
        await _turn(
            handler,
            [HumanMessage(content="Weather?")],
            AIMessage(
                content="Sunny.",
                usage_metadata={
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "total_tokens": 3,
                },
            ),
        )

    assert messages == []
    assert len(emitted) == 1, "usage is a separate stream and still leaves"


@pytest.mark.asyncio
async def test_when_the_model_is_not_a_chat_model_then_prompts_are_still_logged(
    messages: list[dict[str, Any]],
) -> None:
    """``on_llm_start`` is the non-chat path: bare prompt strings in, and a
    bare ``Generation`` with no ``.message`` on the way back."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")
    run_id = uuid4()

    with run_scope("my-agent"):
        await handler.on_llm_start({}, ["Say hi"], run_id=run_id)
        await handler.on_llm_end(
            LLMResult(
                generations=[[Generation(text="hi")]],
                llm_output={"model_name": "gpt-4o"},
            ),
            run_id=run_id,
        )

    [event] = messages
    assert event["input_messages"] == [
        {"role": "user", "parts": [{"type": "text", "content": "Say hi"}]}
    ]
    assert event["output_messages"] == [
        {"role": "assistant", "parts": [{"type": "text", "content": "hi"}]}
    ]


@pytest.mark.asyncio
async def test_when_the_provider_names_no_model_then_a_placeholder_is_used(
    messages: list[dict[str, Any]],
) -> None:
    """The platform rejects an empty ``model`` outright (min_length=1), which
    would drop the whole event rather than just its least interesting field."""
    handler = HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")

    with run_scope("my-agent"):
        await handler.on_llm_end(
            LLMResult(
                generations=[[ChatGeneration(message=AIMessage(content="Sunny."))]],
                llm_output=None,
            ),
            run_id=uuid4(),
        )

    [event] = messages
    assert event["model"] == "default"
