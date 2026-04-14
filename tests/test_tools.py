"""Tests for custom agent tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from asianf.tools.decorators import format_tool_call_label
from asianf.tools.fetch import _get_env_or_raise as get_fetch_env
from asianf.tools.fetch import _format_fetch_call
from asianf.tools.fetch import fetch
from asianf.tools.websearch import _get_env_or_raise as get_search_env
from asianf.tools.websearch import _format_web_search_call
from asianf.tools.websearch import web_search


class DummyResponse:
    """Provide a small stand-in for an HTTP response."""

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store the JSON payload for later access."""
        self._payload = payload

    def raise_for_status(self) -> None:
        """Pretend the HTTP response succeeded."""

    def json(self) -> dict[str, Any]:
        """Return the mocked JSON payload."""
        return self._payload


class DummyAsyncClient:
    """Provide a mock async client for tool tests."""

    def __init__(self, responder: Callable[..., DummyResponse], **_kwargs: Any) -> None:
        """Store the post responder callable."""
        self._responder = responder

    async def __aenter__(self) -> "DummyAsyncClient":
        """Enter the async client context."""
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Exit the async client context."""

    async def post(self, *args: Any, **kwargs: Any) -> DummyResponse:
        """Return the preconfigured response for a POST request."""
        return self._responder(*args, **kwargs)


def test_get_env_or_raise_requires_present_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise when a required environment variable is missing."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("LINKUP_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        get_fetch_env("TAVILY_API_KEY")

    with pytest.raises(RuntimeError, match="LINKUP_API_KEY"):
        get_search_env("LINKUP_API_KEY")


def test_format_tool_call_label_defaults_to_tool_name() -> None:
    """Fall back to the tool name when no custom formatter is attached."""

    class PlainTool:
        """Provide a tiny stand-in tool with only a name."""

        name = "plain_tool"

    assert format_tool_call_label(PlainTool(), {"foo": "bar"}) == "plain_tool"


def test_web_search_call_formatter_is_human_friendly() -> None:
    """Render web search calls with the user query."""
    assert _format_web_search_call({"query": "latest ai breakthroughs"}) == (
        "searching latest ai breakthroughs"
    )
    assert format_tool_call_label(web_search, {"query": "latest ai breakthroughs"}) == (
        "searching latest ai breakthroughs"
    )


def test_fetch_call_formatter_truncates_url() -> None:
    """Render fetch calls with a shortened URL."""
    label = _format_fetch_call(
        {
            "url": (
                "https://example.com/really/long/path/to/article/about/ai/"
                "breakthroughs/index.html"
            )
        }
    )

    assert label.startswith("fetching example.com/")
    assert label.endswith("...")
    assert format_tool_call_label(fetch, {"url": "https://example.com/post"}) == (
        "fetching example.com/post"
    )


@pytest.mark.asyncio
async def test_fetch_returns_trimmed_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalize Tavily results and cap raw content length."""
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    long_content = "x" * 25_000

    def responder(url: str, **kwargs: Any) -> DummyResponse:
        """Return a successful mocked Tavily payload."""
        assert url == "https://api.tavily.com/extract"
        assert kwargs["headers"]["Authorization"] == "Bearer tavily-key"
        assert kwargs["json"]["urls"] == "https://example.com"
        return DummyResponse(
            {
                "results": [
                    {
                        "title": "Example Title",
                        "raw_content": long_content,
                    }
                ]
            }
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: DummyAsyncClient(responder, **kwargs),
    )

    result = await fetch.ainvoke({"url": "https://example.com"})

    assert result["url"] == "https://example.com"
    assert result["title"] == "Example Title"
    assert len(result["content"]) == 20_000


@pytest.mark.asyncio
async def test_web_search_normalizes_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalize Linkup results into the expected response shape."""
    monkeypatch.setenv("LINKUP_API_KEY", "linkup-key")

    def responder(url: str, **kwargs: Any) -> DummyResponse:
        """Return a successful mocked Linkup payload."""
        assert url == "https://api.linkup.so/v1/search"
        assert kwargs["headers"]["Authorization"] == "Bearer linkup-key"
        assert kwargs["json"]["q"] == "langchain agents"
        return DummyResponse(
            {
                "results": [
                    {
                        "name": "LangChain",
                        "url": "https://example.com/langchain",
                        "content": "Agent docs",
                        "favicon": "https://example.com/favicon.ico",
                    },
                    {
                        "name": "LangGraph",
                        "url": "https://example.com/langgraph",
                        "content": "Runtime docs",
                        "favicon": None,
                    },
                ]
            }
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: DummyAsyncClient(responder, **kwargs),
    )

    result = await web_search.ainvoke({"query": "langchain agents", "max_results": 1})

    assert result == {
        "query": "langchain agents",
        "results": [
            {
                "title": "LangChain",
                "url": "https://example.com/langchain",
                "content": "Agent docs",
                "favicon": "https://example.com/favicon.ico",
            }
        ],
    }
