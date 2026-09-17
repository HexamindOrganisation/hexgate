"""Tests for HexgateUsagePlugin's usage extraction and message capture."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from hexgate.adapters.google import usage as usage_mod
from hexgate.adapters.google.usage import HexgateUsagePlugin


def _context(agent_name: str = "my-agent", invocation_id: str = "e-1") -> Any:
    """Minimal duck-typed stand-in for CallbackContext — the hooks only read
    .agent_name and .invocation_id, and a real one needs a full
    InvocationContext."""
    return SimpleNamespace(agent_name=agent_name, invocation_id=invocation_id)


def _response(
    *, prompt_tokens: int | None = 10, candidates_tokens: int | None = 20
) -> LlmResponse:
    return LlmResponse(
        model_version="gemini-2.0-flash",
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidates_tokens,
        ),
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


@pytest.mark.asyncio
async def test_after_model_callback_emits_usage_from_response(
    emitted: list[dict[str, Any]],
) -> None:
    plugin = HexgateUsagePlugin(api_key="k")
    context = _context("my-agent")

    await plugin.before_model_callback(
        callback_context=context, llm_request=_request([_user("a")])
    )
    result = await plugin.after_model_callback(
        callback_context=context, llm_response=_response()
    )

    assert result is None  # never rewrites the response
    [call] = emitted
    assert call == {
        "agent_name": "my-agent",
        "model": "gemini-2.0-flash",
        "input_tokens": 10,
        "output_tokens": 20,
        "api_key": "k",
    }


@pytest.mark.asyncio
async def test_after_model_callback_does_nothing_when_no_usage_metadata(
    emitted: list[dict[str, Any]],
) -> None:
    plugin = HexgateUsagePlugin(api_key="k")
    response = LlmResponse(model_version="gemini-2.0-flash", usage_metadata=None)

    await plugin.before_model_callback(
        callback_context=_context(), llm_request=_request([_user("a")])
    )
    await plugin.after_model_callback(
        callback_context=_context(), llm_response=response
    )

    assert emitted == []


@pytest.mark.asyncio
async def test_after_model_callback_defaults_missing_token_counts_to_zero(
    emitted: list[dict[str, Any]],
) -> None:
    """usage_metadata present but a count field is None (provider-specific
    gaps) must not crash — reported as 0, not skipped."""
    plugin = HexgateUsagePlugin(api_key="k")
    response = _response(prompt_tokens=None, candidates_tokens=None)

    await plugin.before_model_callback(
        callback_context=_context(), llm_request=_request([_user("a")])
    )
    await plugin.after_model_callback(
        callback_context=_context(), llm_response=response
    )

    [call] = emitted
    assert call["input_tokens"] == 0
    assert call["output_tokens"] == 0


# --- Message capture ----------------------------------------------------------


def _request(
    contents: list[types.Content], *, system_instruction: Any = None
) -> LlmRequest:
    return LlmRequest(
        model="gemini-2.0-flash",
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_instruction),
    )


def _user(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def _model_response(text: str = "Sunny, 24C.", **kwargs: Any) -> LlmResponse:
    return LlmResponse(
        model_version="gemini-2.0-flash",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        **kwargs,
    )


@pytest.fixture()
def messages(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture emit_llm_messages() calls without touching the sender registry."""
    calls: list[dict[str, Any]] = []

    def fake_emit(
        agent_name: str,
        model: str,
        input_messages: list[Any],
        output_messages: list[Any],
        **kwargs: Any,
    ) -> None:
        calls.append(
            dict(
                agent_name=agent_name,
                model=model,
                input_messages=input_messages,
                output_messages=output_messages,
                **kwargs,
            )
        )

    monkeypatch.setattr(usage_mod, "emit_llm_messages", fake_emit)
    return calls


async def _turn(
    plugin: HexgateUsagePlugin,
    context: Any,
    request: LlmRequest,
    response: LlmResponse,
) -> None:
    """One model call: the callback pair, in the order ADK fires them."""
    await plugin.before_model_callback(callback_context=context, llm_request=request)
    await plugin.after_model_callback(callback_context=context, llm_response=response)


@pytest.mark.asyncio
async def test_after_model_callback_messages_happy_path(
    messages: list[dict[str, Any]],
) -> None:
    plugin = HexgateUsagePlugin(api_key="k")

    await _turn(
        plugin,
        _context(),
        _request([_user("Weather in Paris?")], system_instruction="Be terse."),
        _model_response(),
    )

    [call] = messages
    assert call == {
        "agent_name": "my-agent",
        "model": "gemini-2.0-flash",
        "input_messages": [
            {
                "role": "user",
                "parts": [{"type": "text", "content": "Weather in Paris?"}],
            }
        ],
        "output_messages": [
            {"role": "assistant", "parts": [{"type": "text", "content": "Sunny, 24C."}]}
        ],
        "turn_key": "e-1:my-agent",
        "message_seq": 0,
        "system_instructions": [{"type": "text", "content": "Be terse."}],
        "resynced": False,
        "api_key": "k",
    }


@pytest.mark.asyncio
async def test_when_a_second_call_extends_the_contents_then_only_the_delta_is_sent(
    messages: list[dict[str, Any]],
) -> None:
    """ADK rebuilds ``contents`` from the whole session on every call, so the
    cursor is what keeps the transcript from repeating itself."""
    plugin = HexgateUsagePlugin(api_key="k")
    first = [_user("Weather in Paris?")]

    await _turn(plugin, _context(), _request(first), _model_response())
    await _turn(
        plugin,
        _context(),
        _request([*first, _user("And in Lyon?")], system_instruction="Be terse."),
        _model_response("Rainy, 16C."),
    )

    assert [call["message_seq"] for call in messages] == [0, 1]
    assert messages[1]["input_messages"] == [
        {"role": "user", "parts": [{"type": "text", "content": "And in Lyon?"}]}
    ]
    # Instructions ride on the first event of a turn_key only.
    assert messages[1]["system_instructions"] is None


@pytest.mark.asyncio
async def test_when_two_invocations_interleave_then_each_keeps_its_own_seq(
    messages: list[dict[str, Any]],
) -> None:
    """An AgentTool runs a nested Runner, so its calls carry a different
    invocation id and must not be filed under the parent's message list."""
    plugin = HexgateUsagePlugin(api_key="k")
    parent, child = _context(invocation_id="e-1"), _context(invocation_id="e-2")

    await _turn(plugin, parent, _request([_user("a")]), _model_response())
    await _turn(plugin, child, _request([_user("b")]), _model_response())
    await _turn(plugin, parent, _request([_user("a"), _user("c")]), _model_response())

    assert [(c["turn_key"], c["message_seq"]) for c in messages] == [
        ("e-1:my-agent", 0),
        ("e-2:my-agent", 0),
        ("e-1:my-agent", 1),
    ]


@pytest.mark.asyncio
async def test_when_contents_are_rewritten_then_the_event_is_resynced(
    messages: list[dict[str, Any]],
) -> None:
    """A trimmed or summarized history cannot be sliced at the old mark."""
    plugin = HexgateUsagePlugin(api_key="k")

    await _turn(
        plugin, _context(), _request([_user("a"), _user("b")]), _model_response()
    )
    await _turn(plugin, _context(), _request([_user("summary")]), _model_response())

    assert [call["resynced"] for call in messages] == [False, True]
    assert messages[1]["input_messages"] == [
        {"role": "user", "parts": [{"type": "text", "content": "summary"}]}
    ]


@pytest.mark.asyncio
async def test_when_the_response_is_partial_then_nothing_is_emitted(
    messages: list[dict[str, Any]],
) -> None:
    """In SSE streaming mode the callback also fires once per chunk; only the
    aggregated response is a completed call."""
    plugin = HexgateUsagePlugin(api_key="k")
    context = _context()

    await plugin.before_model_callback(
        callback_context=context, llm_request=_request([_user("a")])
    )
    await plugin.after_model_callback(
        callback_context=context, llm_response=_model_response("Sun", partial=True)
    )
    await plugin.after_model_callback(
        callback_context=context, llm_response=_model_response("Sunny, 24C.")
    )

    [call] = messages
    assert call["output_messages"][0]["parts"] == [
        {"type": "text", "content": "Sunny, 24C."}
    ]


@pytest.mark.asyncio
async def test_when_message_logging_is_off_then_only_usage_is_emitted(
    emitted: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEXGATE_LOG_MESSAGES", "0")
    plugin = HexgateUsagePlugin(api_key="k")

    await plugin.before_model_callback(
        callback_context=_context(), llm_request=_request([_user("a")])
    )
    await plugin.after_model_callback(
        callback_context=_context(), llm_response=_response()
    )

    assert messages == []
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_when_the_response_carries_no_usage_then_messages_still_emit(
    emitted: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> None:
    """A missing ``usage_metadata`` used to return early — the transcript row
    does not depend on the token counts."""
    plugin = HexgateUsagePlugin(api_key="k")

    await _turn(plugin, _context(), _request([_user("a")]), _model_response())

    assert emitted == []
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_when_conversion_raises_then_the_run_is_not_failed(
    messages: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = HexgateUsagePlugin(api_key="k")

    def boom(_: Any) -> None:
        raise ValueError("bad content")

    monkeypatch.setattr(usage_mod, "input_message", boom)

    await _turn(plugin, _context(), _request([_user("a")]), _model_response())

    assert messages == []


@pytest.mark.asyncio
async def test_when_a_run_ends_then_its_invocation_state_is_dropped(
    messages: list[dict[str, Any]],
) -> None:
    """Without the reset the cursor would hold every message list the plugin
    has ever seen; after it, a reused id starts a fresh turn."""
    plugin = HexgateUsagePlugin(api_key="k")

    await _turn(plugin, _context(), _request([_user("a")]), _model_response())
    await plugin.after_run_callback(
        invocation_context=SimpleNamespace(invocation_id="e-1")
    )
    await _turn(plugin, _context(), _request([_user("b")]), _model_response())

    assert [call["message_seq"] for call in messages] == [0, 0]


@pytest.mark.asyncio
async def test_when_the_response_omits_the_model_then_the_request_model_is_used(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    """ADK's SSE aggregate — the only non-partial response in streaming mode —
    is built without a ``model_version``, and the platform rejects both events
    on an empty model."""
    plugin = HexgateUsagePlugin(api_key="k")
    aggregate = LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text="Sunny, 24C.")]),
        partial=False,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1, candidates_token_count=2
        ),
    )

    await _turn(plugin, _context(), _request([_user("a")]), aggregate)

    assert messages[0]["model"] == "gemini-2.0-flash"
    assert emitted[0]["model"] == "gemini-2.0-flash"


@pytest.mark.asyncio
async def test_when_a_response_has_no_stashed_request_then_no_event_is_emitted(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    """A second non-partial response for one call is not a second model call;
    emitting for it would spend a seq on an empty prompt and flag a resync."""
    plugin = HexgateUsagePlugin(api_key="k")
    context = _context()

    await _turn(plugin, context, _request([_user("a")]), _model_response())
    await plugin.after_model_callback(
        callback_context=context,
        llm_response=_model_response(
            "…and warm.",
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10, candidates_token_count=20
            ),
        ),
    )

    assert len(messages) == 1
    # Usage is guarded by the same stash: on ADK's non-progressive streaming
    # path two responses for one call both read as completed.
    assert emitted == []


@pytest.mark.asyncio
async def test_when_chunks_stream_then_tokens_are_counted_once(
    emitted: list[dict[str, Any]],
) -> None:
    """ADK repeats the call's running counts on every SSE chunk, so counting
    the partials too would bill one answer once per chunk."""
    plugin = HexgateUsagePlugin(api_key="k")
    context = _context()
    counts = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=10, candidates_token_count=20
    )

    await plugin.before_model_callback(
        callback_context=context, llm_request=_request([_user("a")])
    )
    await plugin.after_model_callback(
        callback_context=context,
        llm_response=_model_response("Sun", partial=True, usage_metadata=counts),
    )
    await plugin.after_model_callback(
        callback_context=context,
        llm_response=_model_response("Sunny.", partial=False, usage_metadata=counts),
    )

    assert [(c["input_tokens"], c["output_tokens"]) for c in emitted] == [(10, 20)]


@pytest.mark.asyncio
async def test_when_runs_end_abnormally_then_both_halves_are_bounded(
    messages: list[dict[str, Any]],
) -> None:
    """ADK skips after_run_callback when the agent loop raises or the caller
    stops iterating, so neither half can rely on it alone.

    Each iteration is a whole model call, which is what makes this bite: the
    prompt is released on the completed response, so a cap that reached the
    cursor only through a live prompt would bound nothing.
    """
    plugin = HexgateUsagePlugin(api_key="k")

    for n in range(plugin._MAX_PENDING + 10):
        context = _context(invocation_id=f"e-{n}")
        await _turn(plugin, context, _request([_user("a")]), _model_response())

    assert len(plugin._pending) == plugin._MAX_PENDING
    assert len(plugin._cursor._turns) <= plugin._MAX_PENDING
    assert "e-0:my-agent" not in plugin._pending  # the stalest went first


@pytest.mark.asyncio
async def test_when_sub_agents_share_an_invocation_then_each_keeps_its_own_list(
    messages: list[dict[str, Any]],
) -> None:
    """A ParallelAgent copies the invocation context and changes only `branch`,
    so concurrent sub-agents share an invocation id — keyed on that alone one
    would pop the other's prompt and the second would emit nothing."""
    plugin = HexgateUsagePlugin(api_key="k")
    first, second = _context("researcher", "e-1"), _context("writer", "e-1")

    # Interleaved the way TaskGroup runs them: both requests before either
    # response.
    await plugin.before_model_callback(
        callback_context=first, llm_request=_request([_user("research this")])
    )
    await plugin.before_model_callback(
        callback_context=second, llm_request=_request([_user("write this")])
    )
    await plugin.after_model_callback(
        callback_context=first, llm_response=_model_response()
    )
    await plugin.after_model_callback(
        callback_context=second, llm_response=_model_response()
    )

    assert [(c["agent_name"], c["turn_key"]) for c in messages] == [
        ("researcher", "e-1:researcher"),
        ("writer", "e-1:writer"),
    ]
    # Each row carries its own agent's prompt, not the other's.
    assert messages[0]["input_messages"][0]["parts"][0]["content"] == "research this"
    assert messages[1]["input_messages"][0]["parts"][0]["content"] == "write this"


@pytest.mark.asyncio
async def test_when_a_run_ends_then_every_agent_list_is_dropped(
    messages: list[dict[str, Any]],
) -> None:
    """after_run_callback sees only the invocation id, but one invocation can
    hold a list per agent."""
    plugin = HexgateUsagePlugin(api_key="k")
    for name in ("researcher", "writer"):
        await _turn(
            plugin, _context(name, "e-1"), _request([_user("a")]), _model_response()
        )

    await plugin.after_run_callback(
        invocation_context=SimpleNamespace(invocation_id="e-1")
    )

    assert plugin._pending == {}
    assert plugin._cursor._turns == {}
