"""Tests for HexgateUsageCallbackHandler: usage extraction from LLMResult, and
the ``on_chat_model_start`` → ``on_llm_end`` pair behind the message event."""

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

USAGE = {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}


def _result(
    *,
    usage_metadata: dict | None = None,
    llm_output: dict | None = None,
    message: AIMessage | None = None,
) -> LLMResult:
    message = message or AIMessage(content="hi", usage_metadata=usage_metadata)
    return LLMResult(
        generations=[[ChatGeneration(message=message)]], llm_output=llm_output
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
    """Capture emit_llm_messages() calls, autouse so no test reaches the real
    emit and builds an OTLP sender for the fake api_key."""
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
    """One full LLM call, with a fresh ``run_id`` as LangChain mints one per
    model call."""
    call_id = run_id or uuid4()
    await handler.on_chat_model_start(
        {}, [prompt], run_id=call_id, metadata={"ls_model_name": "gpt-4o"}
    )
    await handler.on_llm_end(
        _result(message=completion, llm_output={"model_name": "gpt-4o"}),
        run_id=call_id,
    )


def _handler() -> HexgateUsageCallbackHandler:
    return HexgateUsageCallbackHandler(agent_name="my-agent", api_key="k")


TOOL_CALL = AIMessage(
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


# --- Usage -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_llm_end_happy_path(emitted: list[dict[str, Any]]) -> None:
    await _handler().on_llm_end(
        _result(usage_metadata=USAGE, llm_output={"model_name": "gpt-4o"}),
        run_id=uuid4(),
    )

    assert emitted == [
        {
            "agent_name": "my-agent",
            "model": "gpt-4o",
            "input_tokens": 10,
            "output_tokens": 20,
            "api_key": "k",
        }
    ]


@pytest.mark.asyncio
async def test_when_only_the_legacy_token_usage_is_set_then_it_is_used(
    emitted: list[dict[str, Any]],
) -> None:
    """Providers that skip the standardized UsageMetadata field still report
    usage via the legacy llm_output shape."""
    await _handler().on_llm_end(
        _result(
            llm_output={
                "model_name": "legacy-model",
                "token_usage": {"prompt_tokens": 5, "completion_tokens": 7},
            }
        ),
        run_id=uuid4(),
    )

    assert (emitted[0]["input_tokens"], emitted[0]["output_tokens"]) == (5, 7)


@pytest.mark.asyncio
async def test_when_no_usage_is_reported_then_no_event_is_emitted(
    emitted: list[dict[str, Any]],
) -> None:
    """A provider that reports no usage must not synthesize a zeroed event."""
    await _handler().on_llm_end(
        _result(llm_output={"model_name": "gpt-4o"}), run_id=uuid4()
    )

    assert emitted == []


@pytest.mark.asyncio
async def test_when_streaming_then_the_model_comes_from_response_metadata(
    emitted: list[dict[str, Any]],
) -> None:
    """Streaming aggregates llm_output to None (confirmed against a real
    ChatOpenAI call), so model_name must come from response_metadata rather
    than "" , which the platform's schema rejects (min_length=1)."""
    message = AIMessage(
        content="hi",
        usage_metadata=USAGE,
        response_metadata={"model_name": "gpt-4o-mini", "finish_reason": "stop"},
    )

    await _handler().on_llm_end(_result(message=message), run_id=uuid4())

    assert emitted[0]["model"] == "gpt-4o-mini"


# --- Messages ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_llm_end_emits_messages_happy_path(
    messages: list[dict[str, Any]],
) -> None:
    """The stashed prompt and the completion leave as one event, under a key
    naming the Hexgate run."""
    handler = _handler()

    with run_scope("my-agent") as facts:
        await _turn(
            handler,
            [SystemMessage(content="Be terse."), HumanMessage(content="Weather?")],
            AIMessage(content="Sunny."),
        )

    [event] = messages
    assert event["turn_key"] == f"{facts.id}:my-agent"
    assert (event["message_seq"], event["resynced"]) == (0, False)
    assert (event["model"], event["api_key"]) == ("gpt-4o", "k")
    # Lifted into its own capped field, since LangChain has no system-prompt
    # argument.
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
    """LangGraph hands the whole conversation to every call, so re-sending it
    would store the transcript once per turn; the tool result in the delta is
    stored nowhere else, since a decision row records the call, not its return."""
    handler = _handler()
    first = [SystemMessage(content="Be terse."), HumanMessage(content="Weather?")]

    with run_scope("my-agent"):
        await _turn(handler, first, TOOL_CALL)
        await _turn(
            handler,
            [*first, TOOL_CALL, ToolMessage(content="21C", tool_call_id="call_1")],
            AIMessage(content="Sunny in Paris."),
        )

    _, second = messages
    assert (second["message_seq"], second["resynced"]) == (1, False)
    # Instructions ride on the first event of the turn only.
    assert second["system_instructions"] is None
    assert [m["role"] for m in second["input_messages"]] == ["assistant", "tool"]
    assert second["input_messages"][1]["parts"][0]["response"] == "21C"


@pytest.mark.asyncio
async def test_when_the_framework_trimmed_the_list_then_the_event_is_resynced(
    messages: list[dict[str, Any]],
) -> None:
    """Chains drop old turns to fit the context window, so slicing at a mark
    that no longer means anything would emit the wrong tail."""
    handler = _handler()

    with run_scope("my-agent"):
        await _turn(
            handler, [HumanMessage(content="Weather?")], AIMessage(content="Sunny.")
        )
        await _turn(
            handler, [HumanMessage(content="Tomorrow?")], AIMessage(content="Rain.")
        )

    _, second = messages
    assert second["resynced"] is True
    assert second["message_seq"] == 1, "a resync keeps counting; it is not a new turn"


@pytest.mark.asyncio
async def test_when_two_runs_share_a_handler_then_turn_keys_differ(
    messages: list[dict[str, Any]],
) -> None:
    """One handler serves every call a proxy makes, so keyed on anything the
    two runs shared, run 2's first call would continue run 1."""
    handler = _handler()

    for _ in range(2):
        with run_scope("my-agent"):
            await _turn(
                handler, [HumanMessage(content="Weather?")], AIMessage(content="Sunny.")
            )
            handler.end_run(handler.turn_key())

    first, second = messages
    assert first["turn_key"] != second["turn_key"]
    assert [event["message_seq"] for event in messages] == [0, 0]


@pytest.mark.asyncio
async def test_end_run_happy_path() -> None:
    """Without it the state of every run the process ever made would
    accumulate for its lifetime."""
    handler = _handler()

    with run_scope("my-agent"):
        await handler.on_chat_model_start(
            {}, [[HumanMessage(content="Weather?")]], run_id=uuid4()
        )
        handler.end_run(handler.turn_key())

        assert handler._cursor._turns == {}
        assert handler._pending == {}


@pytest.mark.asyncio
async def test_when_a_run_ends_in_a_foreign_context_then_a_live_run_is_untouched(
    messages: list[dict[str, Any]],
) -> None:
    """``astream`` puts ``end_run`` inside an async generator that an early
    break finalizes in a different Context, so the key is captured on the way
    in and an abandoned run cannot reset a live one."""
    handler = _handler()

    with run_scope("my-agent"):
        abandoned_key = handler.turn_key()

    with run_scope("my-agent"):
        assert handler.turn_key() != abandoned_key
        await _turn(
            handler, [HumanMessage(content="Weather?")], AIMessage(content="Sunny.")
        )
        handler.end_run(abandoned_key)  # the abandoned run's finally, run late
        await _turn(
            handler,
            [HumanMessage(content="Weather?"), AIMessage(content="Sunny.")],
            AIMessage(content="Still sunny."),
        )

    assert [event["message_seq"] for event in messages] == [0, 1], (
        "the live run kept counting; a swept cursor would restart at 0"
    )
    assert not messages[1]["resynced"]


# --- The two streams are independent ------------------------------------------


@pytest.mark.asyncio
async def test_when_message_logging_is_off_then_only_usage_is_emitted(
    monkeypatch: pytest.MonkeyPatch,
    emitted: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> None:
    """The opt-out must not reach into usage, including the model that rides
    on the same stash, or a provider echoing no model_name would put every
    opted-out project's tokens into one "default" bucket."""
    monkeypatch.setenv(LOG_MESSAGES_ENV, "0")
    handler = _handler()
    run_id = uuid4()

    with run_scope("my-agent"):
        await handler.on_chat_model_start(
            {},
            [[HumanMessage(content="Weather?")]],
            run_id=run_id,
            metadata={"ls_model_name": "claude-sonnet-5"},
        )
        await handler.on_llm_end(_result(usage_metadata=USAGE), run_id=run_id)

    assert messages == []
    assert emitted[0]["model"] == "claude-sonnet-5"
    # The prompt was never copied, and the stash did not leak.
    assert handler._pending == {}


@pytest.mark.asyncio
async def test_when_the_response_names_no_model_then_the_request_side_one_is_used(
    emitted: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> None:
    """The request half always knows what it is calling, while the response
    carries a model only if the provider echoed it."""
    handler = _handler()
    run_id = uuid4()

    with run_scope("my-agent"):
        await handler.on_chat_model_start(
            {},
            [[HumanMessage(content="Weather?")]],
            run_id=run_id,
            metadata={"ls_model_name": "claude-sonnet-5"},
        )
        await handler.on_llm_end(_result(usage_metadata=USAGE), run_id=run_id)

    # Both streams report the same model for one call.
    assert messages[0]["model"] == emitted[0]["model"] == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_when_nothing_names_a_model_then_a_placeholder_is_used(
    messages: list[dict[str, Any]],
) -> None:
    """The platform rejects an empty ``model`` (min_length=1) and would drop
    the whole event over its least interesting field."""
    handler = _handler()
    run_id = uuid4()

    with run_scope("my-agent"):
        # No metadata on the request side and no model on the response.
        await handler.on_chat_model_start(
            {}, [[HumanMessage(content="Weather?")]], run_id=run_id
        )
        await handler.on_llm_end(_result(), run_id=run_id)

    assert messages[0]["model"] == "default"


# --- Degraded inputs ----------------------------------------------------------


@pytest.mark.asyncio
async def test_when_the_prompt_was_not_stashed_then_nothing_is_emitted(
    messages: list[dict[str, Any]],
) -> None:
    """A handler attached mid-stream misses the start. Emitting the completion
    beside an empty input would cost more than that one row: advancing the
    cursor on an empty list clobbers its mark and spends seq 0, which is the
    only event that lifts the system prompt."""
    handler = _handler()

    with run_scope("my-agent"):
        await handler.on_llm_end(
            _result(message=AIMessage(content="Sunny.")), run_id=uuid4()
        )
        # The next real call must still be this turn's first event.
        await _turn(
            handler,
            [SystemMessage(content="Be terse."), HumanMessage(content="Weather?")],
            AIMessage(content="Sunny."),
        )

    [event] = messages
    assert (event["message_seq"], event["resynced"]) == (0, False)
    assert event["system_instructions"] == [{"type": "text", "content": "Be terse."}]


@pytest.mark.asyncio
async def test_when_the_call_failed_then_its_stash_is_dropped(
    messages: list[dict[str, Any]],
) -> None:
    """No ``on_llm_end`` follows a provider error, so the stash would sit
    there for the life of the proxy."""
    handler = _handler()
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

    with run_scope("my-agent"):
        await _turn(
            _handler(),
            [HumanMessage(content="Weather?")],
            AIMessage(content="Sunny.", usage_metadata=USAGE),
        )

    assert messages == []
    assert len(emitted) == 1, "usage is a separate stream and still leaves"


@pytest.mark.asyncio
async def test_when_the_model_is_not_a_chat_model_then_prompts_are_still_logged(
    messages: list[dict[str, Any]],
) -> None:
    """``on_llm_start`` is the non-chat path, with bare prompt strings in and
    a ``Generation`` carrying no ``.message`` on the way back."""
    handler = _handler()
    run_id = uuid4()

    with run_scope("my-agent"):
        await handler.on_llm_start({}, ["Say hi"], run_id=run_id)
        await handler.on_llm_end(
            LLMResult(generations=[[Generation(text="hi")]]), run_id=run_id
        )

    assert messages[0]["input_messages"] == [
        {"role": "user", "parts": [{"type": "text", "content": "Say hi"}]}
    ]
    assert messages[0]["output_messages"] == [
        {"role": "assistant", "parts": [{"type": "text", "content": "hi"}]}
    ]
