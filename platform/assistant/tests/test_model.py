import json

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.test import TestModel

from hexgate_assistant.model import MODEL_REQUEST_ATTEMPTS, build_model

pytestmark = pytest.mark.anyio

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
    """Answers each request with the next status of ``statuses``; records them."""

    def __init__(self, *statuses: int):
        self.statuses = list(statuses)
        self.requests: list[httpx2.Request] = []

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status = self.statuses.pop(0)
        if status == 200:
            return httpx2.Response(200, json=COMPLETION)
        # Retry-After 0 keeps the test fast and exercises the header path.
        return httpx2.Response(
            status, headers={"retry-after": "0"}, json={"error": {"message": "busy"}}
        )


async def ask(openrouter: OpenRouter) -> str:
    model = build_model(MODEL, "sk-or-test", transport=openrouter.transport())
    return (await Agent(model).run("hi")).output


async def test_build_model_happy_path():
    openrouter = OpenRouter(200)

    assert await ask(openrouter) == "hello"

    [request] = openrouter.requests
    assert request.headers["authorization"] == "Bearer sk-or-test"
    body = json.loads(request.content)
    assert body["model"] == "anthropic/claude-sonnet-4.5"
    assert body["provider"] == {
        "only": ["anthropic"],
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
    assert len(openrouter.requests) == 1 + 3


@pytest.mark.parametrize("status", [400, 401])
async def test_when_openrouter_answers_a_client_error_then_it_is_not_retried(
    status: int,
):
    openrouter = OpenRouter(status, 200)

    with pytest.raises(ModelHTTPError) as error:
        await ask(openrouter)

    assert error.value.status_code == status
    assert len(openrouter.requests) == 1


def test_when_the_model_is_not_openrouter_then_pydantic_ai_resolves_it():
    assert isinstance(build_model("test", None), TestModel)


def test_when_an_openrouter_model_has_no_key_then_build_model_refuses():
    with pytest.raises(ValueError, match="HEXGATE_ASSISTANT_OPENROUTER_KEY"):
        build_model(MODEL, None)


@pytest.mark.parametrize("slug", ["claude-sonnet-4.5", "/claude-sonnet-4.5"])
def test_when_an_openrouter_model_has_no_vendor_then_build_model_refuses(slug: str):
    with pytest.raises(ValueError, match="vendor/model"):
        build_model(f"openrouter:{slug}", "sk-or-test")
