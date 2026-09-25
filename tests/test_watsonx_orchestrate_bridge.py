"""Tests for the watsonx Orchestrate bridge example (examples/watsonx_orchestrate)."""

from __future__ import annotations

import json

import pytest

from examples.watsonx_orchestrate.sse import (
    DONE,
    completion_body,
    last_user_message,
    to_agent_input,
    to_sse,
)
from hexgate.streaming import (
    AgentRunResult,
    BlockDeltaEvent,
    BlockType,
    ErrorEvent,
    RunEndEvent,
    RunStartEvent,
    ToolEndEvent,
    ToolStartEvent,
)

RUN = {"run_id": "run-1", "root_run_id": "run-1", "sequence": 1}


def _payload(frame: str) -> dict:
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    return json.loads(frame[len("data: ") :])


def test_to_agent_input_keeps_chat_turns_only() -> None:
    """Drop tool turns and empty/non-string content Orchestrate may send."""
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "restart web-1 in dev"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
        {"role": "tool", "content": "done", "tool_call_id": "x"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "  "},
    ]

    assert to_agent_input(messages) == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "restart web-1 in dev"},
        {"role": "assistant", "content": "ok"},
    ]


def test_last_user_message() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "second"},
    ]

    assert last_user_message(messages) == "second"
    assert last_user_message([]) == ""


def test_text_delta_becomes_message_delta() -> None:
    event = BlockDeltaEvent(**RUN, block_id="b", block_type=BlockType.TEXT, text="Hi")

    payload = _payload(to_sse(event, thread_id="t-1", model="m"))

    assert payload["object"] == "thread.message.delta"
    assert payload["thread_id"] == "t-1"
    assert payload["choices"][0]["delta"] == {"role": "assistant", "content": "Hi"}


def test_reasoning_delta_becomes_thinking_step() -> None:
    event = BlockDeltaEvent(
        **RUN, block_id="b", block_type=BlockType.REASONING, text="hmm"
    )

    payload = _payload(to_sse(event, thread_id="t", model="m"))

    assert payload["object"] == "thread.run.step.delta"
    assert payload["choices"][0]["delta"]["step_details"] == {
        "type": "thinking",
        "content": "hmm",
    }


def test_tool_call_and_response_share_the_id() -> None:
    """Orchestrate pairs tool_response.tool_call_id with tool_calls[].id."""
    start = ToolStartEvent(
        **RUN,
        tool_id="call-7",
        tool_name="restart_service",
        arguments={"service": "web-1", "env": "dev"},
    )
    end = ToolEndEvent(
        **RUN, tool_id="call-7", tool_name="restart_service", output_summary="restarted"
    )

    call = _payload(to_sse(start, thread_id="t", model="m"))["choices"][0]["delta"][
        "step_details"
    ]
    response = _payload(to_sse(end, thread_id="t", model="m"))["choices"][0]["delta"][
        "step_details"
    ]

    assert call == {
        "type": "tool_calls",
        "tool_calls": [
            {
                "id": "call-7",
                "name": "restart_service",
                "args": {"service": "web-1", "env": "dev"},
            }
        ],
    }
    assert response["type"] == "tool_response"
    assert response["tool_call_id"] == call["tool_calls"][0]["id"]
    assert response["content"] == "restarted"


def test_error_is_shown_as_text_and_unmapped_events_are_skipped() -> None:
    error = _payload(
        to_sse(ErrorEvent(**RUN, message="boom"), thread_id="t", model="m")
    )

    assert error["object"] == "thread.message.delta"
    assert "boom" in error["choices"][0]["delta"]["content"]
    assert to_sse(RunStartEvent(**RUN, query="q"), thread_id="t", model="m") is None


def test_completion_body_shape() -> None:
    body = completion_body("done", model="m")

    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "done"}
    assert body["choices"][0]["finish_reason"] == "stop"


# --- FastAPI app (skipped when fastapi is not installed) --------------------


@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from examples.watsonx_orchestrate.app import create_app

    seen: dict = {}

    async def fake_stream(items, ctx, query):
        seen.update(items=items, ctx=ctx, query=query)
        yield ToolStartEvent(
            **RUN,
            tool_id="c1",
            tool_name="read_logs",
            arguments={"service": "api", "env": "dev"},
        )
        yield ToolEndEvent(
            **RUN, tool_id="c1", tool_name="read_logs", output_summary="200 OK"
        )
        yield BlockDeltaEvent(
            **RUN, block_id="b", block_type=BlockType.TEXT, text="Logs look fine."
        )
        yield RunEndEvent(
            **RUN,
            result=AgentRunResult(
                run_id="run-1", root_run_id="run-1", message="Logs look fine."
            ),
        )

    app = create_app(
        stream=fake_stream, model="m", token="s3cret", user_roles=["operator"]
    )
    return TestClient(app), seen


BODY = {"model": "x", "messages": [{"role": "user", "content": "logs for api in dev?"}]}


def test_rejects_missing_or_wrong_token(client) -> None:
    http, _ = client

    assert http.post("/chat/completions", json=BODY).status_code == 401
    assert (
        http.post(
            "/chat/completions", json=BODY, headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )


def test_accepts_api_key_header_and_returns_completion(client) -> None:
    http, seen = client

    response = http.post(
        "/chat/completions",
        json=BODY,
        headers={"x-api-key": "s3cret", "X-IBM-THREAD-ID": "thread-9"},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Logs look fine."
    assert seen["ctx"].session_id == "thread-9"
    assert seen["ctx"].user_roles == ["operator"]
    assert seen["query"] == "logs for api in dev?"


def test_streams_orchestrate_events_then_done(client) -> None:
    http, _ = client

    response = http.post(
        "/chat/completions",
        json={**BODY, "stream": True},
        headers={"Authorization": "Bearer s3cret"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [f + "\n\n" for f in response.text.split("\n\n") if f]
    assert frames[-1] == DONE
    kinds = [
        _payload(f)["choices"][0]["delta"].get("step_details", {}).get("type", "text")
        for f in frames[:-1]
    ]
    assert kinds == ["tool_calls", "tool_response", "text"]
