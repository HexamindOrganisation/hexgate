"""Register example code-defined agents for the CLI and demos."""

from __future__ import annotations

from pathlib import Path

from coolagents import AgentPolicy, create_agent, enforce_policy, register_agent
from coolagents.tools import fetch, web_search


def _shared_policy() -> AgentPolicy:
    """Return a simple shared policy for example code agents."""
    return AgentPolicy.model_validate(
        {
            "version": 1,
            "default_policy": {"mode": "deny"},
            "tools": {
                "web_search": {"mode": "allow"},
                "fetch": {"mode": "allow"},
            },
        }
    )


def build_website_analyser(
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: dict[str, object] | None = None,
    model: str | None = None,
):
    """Build a code-defined website analysis agent without policy concerns."""
    _ = base_dir
    _ = user_id
    tools = [web_search, fetch]
    if extra_tools:
        tools.extend(tool for tool in extra_tools.values() if tool not in tools)
    return create_agent(
        model=model or "gpt-5.4",
        tools=tools,
        system_prompt=(
            "You analyze websites and landing pages. "
            "Use web_search to find the official site and fetch to inspect its content, "
            "then summarize structure, messaging, product, and trust signals."
        ),
        session_id=session_id,
        tags=tags or ["coolagents", "website-analyser"],
    )


def build_news_collector(
    *,
    base_dir: str | Path | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    extra_tools: dict[str, object] | None = None,
    model: str | None = None,
):
    """Build a code-defined news collection agent with hosted policy enforcement."""
    _ = base_dir
    _ = user_id
    tools = [web_search, fetch]
    if extra_tools:
        tools.extend(tool for tool in extra_tools.values() if tool not in tools)
    agent, handler = create_agent(
        model=model or "gpt-5.4",
        tools=tools,
        system_prompt=(
            "You collect and verify recent news. "
            "Use web_search to identify current coverage, fetch the strongest sources, "
            "and return a concise source-backed roundup."
        ),
        session_id=session_id,
        tags=tags or ["coolagents", "news-collector"],
    )
    return enforce_policy(agent, _shared_policy()), handler


register_agent("website_analyser", build_website_analyser)
register_agent("news_collector", build_news_collector)
