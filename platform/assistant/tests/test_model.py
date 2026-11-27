import json

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from hexgate_assistant.model import (
    MODEL_REQUEST_ATTEMPTS,
    PROVIDERS_BY_VENDOR,
    build_model,
    session_settings,
)

MODEL = "openrouter:anthropic/claude-sonnet-4.5"

COMPLETION = {
    "id": "gen-1",
    "object": "chat.completion",
    "created": 0,
    "model": "anthropic/claude-sonnet-4.5",
    "provider": "Anthropic",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "hello"},
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class OpenRouter:
    """Answers each request with the next of ``statuses`` (a status, or a 200
    with that body); records them."""

    def __init__(self, *statuses: int | dict):
        self.statuses = list(statuses)
        self.requests: list[httpx2.Request] = []

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status = self.statuses.pop(0)
        if isinstance(status, dict):
            return httpx2.Response(200, json=status)
        if status == 200:
            return httpx2.Response(200, json=COMPLETION)
        # Retry-After 0 keeps the test fast and exercises the header path.
        return httpx2.Response(
            status, headers={"retry-after": "0"}, json={"error": {"message": "busy"}}
        )


async def ask(openrouter: OpenRouter, name: str = MODEL) -> str:
    model = build_model(name, "sk-or-test", transport=openrouter.transport())
    return (await Agent(model).run("hi")).output


async def test_build_model_happy_path():
    openrouter = OpenRouter(200)

    assert await ask(openrouter) == "hello"

    [request] = openrouter.requests
    assert request.headers["authorization"] == "Bearer sk-or-test"
    body = json.loads(request.content)
    assert body["model"] == "anthropic/claude-sonnet-4.5"
    assert body["provider"] == {
        "only": ["anthropic", "amazon-bedrock", "google-vertex"],
        "allow_fallbacks": False,
        "data_collection": "deny",
        "zdr": True,
    }


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_when_openrouter_answers_a_retryable_status_then_the_request_is_retried(
    status: int,
):
    openrouter = OpenRouter(status, 200)

    assert await ask(openrouter) == "hello"
    assert len(openrouter.requests) == 2


async def test_when_every_try_is_429_then_first_try_and_3_retries_then_the_error_surfaces():
    openrouter = OpenRouter(*[429] * (MODEL_REQUEST_ATTEMPTS + 1))

    with pytest.raises(ModelHTTPError) as error:
        await ask(openrouter)

    assert error.value.status_code == 429
    assert len(openrouter.requests) == MODEL_REQUEST_ATTEMPTS


@pytest.mark.parametrize("status", [400, 401])
async def test_when_openrouter_answers_a_client_error_then_it_is_not_retried(
    status: int,
):
    openrouter = OpenRouter(status, 200)

    with pytest.raises(ModelHTTPError) as error:
        await ask(openrouter)

    assert error.value.status_code == status
    assert len(openrouter.requests) == 1


@pytest.mark.parametrize(
    ("name", "kind"),
    [("openai:gpt-x", OpenAIResponsesModel), ("openai-chat:llama", OpenAIChatModel)],
)
def test_when_the_model_is_openai_compatible_then_it_carries_no_openrouter_routing(
    monkeypatch, name: str, kind: type
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    model = build_model(name, None)

    assert type(model) is kind
    assert "openrouter_provider" not in (model.settings or {})


@pytest.mark.parametrize(
    "name", ["anthropic:claude-x", "bedrock:anthropic.claude-x", "mistral:m", "test"]
)
def test_when_the_model_prefix_is_not_supported_then_build_model_refuses(name: str):
    with pytest.raises(ValueError, match="openrouter:, openai:, openai-chat:"):
        build_model(name, "sk-or-test")


def test_when_an_openrouter_model_has_no_key_then_build_model_refuses():
    with pytest.raises(ValueError, match="HEXGATE_ASSISTANT_OPENROUTER_KEY"):
        build_model(MODEL, None)


@pytest.mark.parametrize(
    "slug", ["claude-sonnet-4.5", "/claude-sonnet-4.5", "anthropic/"]
)
def test_when_an_openrouter_model_is_not_vendor_slash_model_then_build_model_refuses(
    slug: str,
):
    with pytest.raises(ValueError, match="vendor/model"):
        build_model(f"openrouter:{slug}", "sk-or-test")


@pytest.mark.parametrize("vendor", sorted(PROVIDERS_BY_VENDOR))
async def test_when_a_vendor_is_mapped_then_the_request_goes_only_to_its_providers(
    vendor: str,
):
    openrouter = OpenRouter(200)

    await ask(openrouter, f"openrouter:{vendor}/some-model")

    body = json.loads(openrouter.requests[0].content)
    assert body["provider"]["only"] == PROVIDERS_BY_VENDOR[vendor]


@pytest.mark.parametrize("vendor", ["google", "x-ai", "mistralai", "meta-llama"])
def test_when_a_vendor_is_not_mapped_then_build_model_refuses(vendor: str):
    with pytest.raises(ValueError, match=f"'{vendor}'.*model.py"):
        build_model(f"openrouter:{vendor}/some-model", "sk-or-test")


TOOL_CALL = {
    **COMPLETION,
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "look", "arguments": "{}"},
                    }
                ],
            },
        }
    ],
}


def cached(block: dict) -> bool:
    return block.get("cache_control", {}).get("type") == "ephemeral"


def unmarked(messages: list[dict]) -> list[dict]:
    """`messages` without cache markers, each text as a list of blocks."""

    def blocks(content):
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if isinstance(content, list):
            return [
                {k: v for k, v in b.items() if k != "cache_control"} for b in content
            ]
        return content

    return [m | {"content": blocks(m.get("content"))} for m in messages]


def last_block(message: dict) -> dict:
    content = message["content"]
    return content[-1] if isinstance(content, list) else {}


async def test_when_a_session_runs_two_turns_then_every_request_carries_its_cache_breakpoints():
    openrouter = OpenRouter(TOOL_CALL, COMPLETION, COMPLETION)

    model = build_model(MODEL, "sk-or-test", transport=openrouter.transport())
    agent = Agent(model, instructions="the instructions")
    agent.tool_plain(lambda: "looked", name="look")
    settings = session_settings(model, "session-1")

    # The runner passes the reference as run-level instructions, after the
    # agent's own.
    run = {"instructions": "the reference", "model_settings": settings}
    first = await agent.run("one", **run)
    await agent.run("two", message_history=first.all_messages(), **run)

    requests = [json.loads(r.content) for r in openrouter.requests]
    # Two requests in the first turn (the tool call, then the answer), one in
    # the second.
    assert len(requests) == 3
    for body in requests:
        assert body["session_id"] == "session-1"
        messages = body["messages"]
        split = next(i for i, m in enumerate(messages) if m["role"] != "system")
        system, conversation = messages[:split], messages[split:]
        # One breakpoint, after the reference: both instructions are cached.
        assert "the reference" in json.dumps(system[-1])
        assert cached(last_block(system[-1]))
        assert not any(cached(last_block(m)) for m in system[:-1])
        assert cached(body["tools"][-1])
        assert cached(last_block(conversation[-1]))
        # The message breakpoint is on the last message (a lookback point is
        # added only once more than 18 blocks follow the previous request's
        # end, which these short turns never reach).
        assert not any(cached(last_block(m)) for m in conversation[:-1])
    # Each request resends the one before it unchanged, cache markers aside,
    # and adds to its end: that prefix is what the provider reads back.
    for earlier, later in zip(requests, requests[1:]):
        sent = unmarked(earlier["messages"])
        assert unmarked(later["messages"])[: len(sent)] == sent


@pytest.mark.parametrize("name", ["openai:gpt-x", "openai-chat:llama"])
def test_when_the_model_is_not_openrouter_then_session_settings_add_nothing(
    monkeypatch, name: str
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    assert session_settings(build_model(name, None), "session-1") == {}
