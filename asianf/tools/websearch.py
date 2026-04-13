"""Linkup-backed web search tool."""

from __future__ import annotations

import os

import httpx

from asianf.tracing.langfuse import observe
from asianf.utils.retry import async_retry

try:
    from langchain_core.tools import tool
except Exception:  # pragma: no cover - dependency absent during scaffold phase
    def tool(func=None, *args, **kwargs):
        if func is not None and callable(func):
            return func

        def decorator(inner):
            return inner

        return decorator


def _get_env_or_raise(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


@tool
@observe(name="linkup_web_search")
@async_retry(retries=3, delay_ms=1000, exceptions=(httpx.HTTPError,))
async def web_search(
    query: str,
    max_results: int = 8,
    depth: str = "standard",
) -> dict:
    """Search the web for fresh public information using Linkup."""
    url = "https://api.linkup.so/v1/search"
    api_key = _get_env_or_raise("LINKUP_API_KEY")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "q": query,
        "outputType": "searchResults",
        "depth": depth,
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()

    results = response.json().get("results", [])
    normalized = [
        {
            "title": result.get("name", ""),
            "url": result.get("url", ""),
            "content": result.get("content", ""),
            "favicon": result.get("favicon"),
        }
        for result in results[:max_results]
    ]
    return {
        "query": query,
        "results": normalized,
    }
