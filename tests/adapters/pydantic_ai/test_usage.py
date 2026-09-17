"""Tests for the per-run usage and message events emitted from pydantic_ai
run results."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from hexgate.adapters.pydantic_ai import usage as usage_mod
from hexgate.adapters.pydantic_ai.usage import emit_run_messages, emit_run_usage
from hexgate.runtime import run_scope


class _FakeResult:
    """Minimal stand-in for AgentRunResult/StreamedRunResult/AgentRun."""

    def __init__(
        self, *, input_tokens: int = 10, output_tokens: int = 20, response: Any = None
    ) -> None:
        self._usage = RunUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        self.response = response

    def usage(self) -> RunUsage:
        return self._usage


class _FakePropertyResult(_FakeResult):
    """usage exposed as a property — pydantic_ai 2.x form (RunUsage, non-callable)."""

    @property
    def usage(self) -> RunUsage:  # type: ignore[override]
        return self._usage


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


def test_emit_run_usage_reads_model_from_response_when_available(
    emitted: list[dict[str, Any]],
) -> None:
    """The run's actual response model wins over the agent's static config
    — pydantic_ai supports per-call model overrides."""
    agent = Agent(model=TestModel())  # agent's own model would be "test"
    response = SimpleNamespace(model_name="gpt-4o")
    result = _FakeResult(input_tokens=10, output_tokens=20, response=response)

    emit_run_usage("my-agent", agent, result, api_key="k")

    [call] = emitted
    assert call == {
        "agent_name": "my-agent",
        "model": "gpt-4o",
        "input_tokens": 10,
        "output_tokens": 20,
        "api_key": "k",
    }


def test_emit_run_usage_falls_back_to_agent_model_when_response_has_no_model_name(
    emitted: list[dict[str, Any]],
) -> None:
    agent = Agent(model=TestModel())
    result = _FakeResult(response=SimpleNamespace(model_name=None))

    emit_run_usage("my-agent", agent, result, api_key="k")

    [call] = emitted
    assert call["model"] == "test"


def test_emit_run_usage_falls_back_to_agent_model_when_response_is_none(
    emitted: list[dict[str, Any]],
) -> None:
    agent = Agent(model="some-model", defer_model_check=True)
    result = _FakeResult(response=None)

    emit_run_usage("my-agent", agent, result, api_key="k")

    [call] = emitted
    assert call["model"] == "some-model"


def test_emit_run_usage_when_agent_has_no_model_then_model_is_empty(
    emitted: list[dict[str, Any]],
) -> None:
    agent = Agent()  # no model configured at all
    result = _FakeResult(response=None)

    emit_run_usage("my-agent", agent, result, api_key="k")

    [call] = emitted
    assert call["model"] == ""


def test_emit_run_usage_handles_usage_exposed_as_property(
    emitted: list[dict[str, Any]],
) -> None:
    """pydantic_ai 2.x exposes usage as a property, not a callable method —
    both forms must resolve without raising."""
    agent = Agent(model=TestModel())
    response = SimpleNamespace(model_name="gpt-4o")
    result = _FakePropertyResult(input_tokens=10, output_tokens=20, response=response)

    emit_run_usage("my-agent", agent, result, api_key="k")

    [call] = emitted
    assert call == {
        "agent_name": "my-agent",
        "model": "gpt-4o",
        "input_tokens": 10,
        "output_tokens": 20,
        "api_key": "k",
    }


class _FakeRun(_FakeResult):
    """Adds the run's own messages, which every result shape exposes."""

    def __init__(self, history: list[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._history = history if history is not None else _HISTORY

    def new_messages(self) -> list[Any]:
        return self._history


_HISTORY = [
    ModelRequest(parts=[UserPromptPart("hello")], instructions="Be terse."),
    ModelResponse(parts=[TextPart("hi")]),
]


@pytest.fixture()
def messages(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture emit_llm_messages() calls without touching the sender registry."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        usage_mod,
        "emit_llm_messages",
        lambda *args, **kwargs: calls.append(dict(zip(_EMIT_ARGS, args)) | kwargs),
    )
    return calls


_EMIT_ARGS = ("agent_name", "model", "input_messages", "output_messages")


def test_emit_run_messages_happy_path(messages: list[dict[str, Any]]) -> None:
    agent = Agent(model=TestModel())
    run = _FakeRun(response=SimpleNamespace(model_name="gpt-4o"))

    with run_scope("my-agent") as facts:
        emit_run_messages("my-agent", agent, run, api_key="k")

    [call] = messages
    assert call == {
        "agent_name": "my-agent",
        "model": "gpt-4o",
        "input_messages": [
            {"role": "user", "parts": [{"type": "text", "content": "hello"}]}
        ],
        "output_messages": [
            {"role": "assistant", "parts": [{"type": "text", "content": "hi"}]}
        ],
        # One event per run, so the run is the message list and there is no
        # earlier event for this key to extend.
        "turn_key": facts.id,
        "message_seq": 0,
        "system_instructions": [{"type": "text", "content": "Be terse."}],
        "api_key": "k",
    }


def test_when_there_is_no_run_scope_then_the_turn_key_is_still_unique(
    messages: list[dict[str, Any]],
) -> None:
    """Sharing a key at seq 0 would read as one run's row duplicated."""
    agent = Agent(model=TestModel())

    for _ in range(2):
        emit_run_messages("my-agent", agent, _FakeRun(), api_key="k")

    assert messages[0]["turn_key"] != messages[1]["turn_key"]


def test_when_message_logging_is_off_then_nothing_is_emitted(
    messages: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEXGATE_LOG_MESSAGES", "0")

    emit_run_messages("my-agent", Agent(model=TestModel()), _FakeRun(), api_key="k")

    assert messages == []


def test_when_the_history_cannot_be_read_then_the_run_is_not_failed(
    messages: list[dict[str, Any]],
) -> None:
    """The run methods do not guard this call, and losing a transcript row
    must not fail the run it was logging."""

    class _Broken(_FakeRun):
        def new_messages(self) -> list[Any]:
            raise RuntimeError("boom")

    emit_run_messages("my-agent", Agent(model=TestModel()), _Broken(), api_key="k")

    assert messages == []


def test_when_a_run_completes_then_usage_and_messages_report_one_model(
    emitted: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    """Both events resolve the model the same way, from one call site."""
    agent = Agent(model=TestModel())
    run = _FakeRun(response=SimpleNamespace(model_name="gpt-4o"))

    emit_run_usage("my-agent", agent, run, api_key="k")
    emit_run_messages("my-agent", agent, run, api_key="k")

    assert emitted[0]["model"] == messages[0]["model"] == "gpt-4o"


def test_when_the_run_produced_nothing_then_no_event_is_emitted(
    messages: list[dict[str, Any]],
) -> None:
    """A stream aborted before the first token has no prompt to record."""
    emit_run_messages(
        "my-agent", Agent(model=TestModel()), _FakeRun(history=[]), api_key="k"
    )

    assert messages == []
