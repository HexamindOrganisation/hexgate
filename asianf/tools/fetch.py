"""Tavily-backed URL fetch tool."""

from __future__ import annotations

import os

import httpx
from langchain_core.tools import tool

from asianf.tracing.langfuse import observe
from asianf.utils.retry import async_retry


def _get_env_or_raise(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


@tool
@observe(name="tavily_fetch")
@async_retry(retries=3, delay_ms=1000, exceptions=(httpx.HTTPError,))
async def fetch(
    url: str,
    extract_depth: str = "basic",
) -> dict:
    """Fetch and extract the contents of a specific URL."""
    api_key = _get_env_or_raise("TAVILY_API_KEY")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "urls": url,
        "extract_depth": extract_depth,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.tavily.com/extract",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()

    raw = response.json().get("results", [{}])[0]
    content = raw.get("raw_content", "") or ""
    return {
        "url": url,
        "title": raw.get("title"),
        "content": content[:20_000],
    }
