"""The model the assistant talks to, built once at start-up from its model
string.

An ``openrouter:`` model runs on our key and is pinned to its own vendor:
no fallback to another provider, none that keeps or trains on the data.
An ``openai:`` model (OpenAI's Responses API) or an ``openai-chat:`` model
(Chat Completions: OpenAI, or a self-hosted OpenAI-compatible server such as
vLLM or Ollama) goes to Pydantic AI's own lookup, which reads OpenAI's
standard environment variables. Every other prefix is refused at start-up:
these three are the supported ones.
"""

import httpx2
from pydantic_ai.models import Model, create_async_httpx2_client, infer_model
from pydantic_ai.models.openrouter import (
    OpenRouterModel,
    OpenRouterModelSettings,
    OpenRouterProviderConfig,
)
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.retries import (
    AsyncHTTPX2TenacityTransport,
    RetryConfig,
    wait_retry_after,
)
from tenacity import RetryCallState, retry_if_exception_type, stop_after_attempt

OPENROUTER_PREFIX = "openrouter:"
# The self-hosted prefixes supported; both need only the OpenAI SDK, which
# the openrouter extra installs.
OPENAI_PREFIXES = ("openai:", "openai-chat:")

# The first try and 3 retries, on 429, 5xx and overloaded (529).
MODEL_REQUEST_ATTEMPTS = 4
# A longer Retry-After is cut to this. The request's deadline (agent.py)
# bounds its attempts and waits together; nothing here bounds the turn.
MAX_RETRY_WAIT_SECONDS = 30

# OpenRouter provider slugs per model vendor (the part of the model slug
# before the "/"). The two names are not always the same, so each vendor is
# listed by hand once its slugs are checked against OpenRouter's provider
# list. A vendor missing here is refused at start-up, never routed by guess.
PROVIDERS_BY_VENDOR: dict[str, list[str]] = {
    "anthropic": ["anthropic"],
    "openai": ["openai"],
}


def build_model(
    name: str,
    openrouter_key: str | None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Model:
    """The model for ``name``. ``transport`` replaces the network in tests."""
    if name.startswith(OPENAI_PREFIXES):
        return infer_model(name)
    if not name.startswith(OPENROUTER_PREFIX):
        supported = ", ".join((OPENROUTER_PREFIX, *OPENAI_PREFIXES))
        raise ValueError(
            f"HEXGATE_ASSISTANT_MODEL {name!r} is not supported: use one of {supported}"
        )
    if openrouter_key is None:
        raise ValueError(
            f"HEXGATE_ASSISTANT_OPENROUTER_KEY is required for {OPENROUTER_PREFIX} models"
        )
    slug = name.removeprefix(OPENROUTER_PREFIX)
    client = create_async_httpx2_client(transport=retrying_transport(transport))
    return OpenRouterModel(
        slug,
        provider=OpenRouterProvider(api_key=openrouter_key, http_client=client),
        settings=OpenRouterModelSettings(
            openrouter_provider=provider_routing(slug),
            # The transport retries; the SDK's own retries would multiply them.
            max_retries=0,
        ),
    )


def provider_routing(slug: str) -> OpenRouterProviderConfig:
    """Route ``vendor/model`` to the vendor's own providers alone, with no
    fallback and no data kept by them."""
    vendor, sep, model = slug.partition("/")
    if not sep or not vendor or not model:
        raise ValueError(f"OpenRouter model {slug!r} is not of the form vendor/model")
    if vendor not in PROVIDERS_BY_VENDOR:
        raise ValueError(
            f"OpenRouter vendor {vendor!r} has no provider slugs: add it to "
            "PROVIDERS_BY_VENDOR in hexgate_assistant/model.py"
        )
    return OpenRouterProviderConfig(
        only=list(PROVIDERS_BY_VENDOR[vendor]),
        allow_fallbacks=False,
        data_collection="deny",
        zdr=True,
    )


def retrying_transport(
    wrapped: httpx2.AsyncBaseTransport | None = None,
) -> AsyncHTTPX2TenacityTransport:
    """Retry a model request on 429 and 5xx, honouring Retry-After.

    After the last attempt the failing response itself is returned, so the
    provider SDK raises its usual status error rather than a transport one.
    """
    return AsyncHTTPX2TenacityTransport(
        RetryConfig(
            retry=retry_if_exception_type(httpx2.HTTPStatusError),
            wait=wait_retry_after(max_wait=MAX_RETRY_WAIT_SECONDS),
            stop=stop_after_attempt(MODEL_REQUEST_ATTEMPTS),
            retry_error_callback=_last_response,
        ),
        wrapped=wrapped,
        validate_response=_raise_if_retryable,
    )


def _raise_if_retryable(response: httpx2.Response) -> None:
    if response.status_code == 429 or response.status_code >= 500:
        response.raise_for_status()


def _last_response(state: RetryCallState) -> httpx2.Response:
    assert state.outcome is not None
    error = state.outcome.exception()
    assert isinstance(error, httpx2.HTTPStatusError)
    return error.response
