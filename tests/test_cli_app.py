"""Tests for inline terminal app rendering helpers."""

from __future__ import annotations

from rich.console import Console

from coolagents.agents import loader
from coolagents.cli.app import (
    AgentRuntime,
    DOG_LOGO,
    _load_agent_script,
    _render_welcome,
    _tail_text,
)


def test_tail_text_keeps_last_lines_of_long_output() -> None:
    """Keep only the trailing lines for live rendering."""
    text = "\n".join(f"line {index}" for index in range(1, 21))

    tailed, truncated = _tail_text(text, max_lines=4, max_chars=10_000)

    assert tailed == "line 17\nline 18\nline 19\nline 20"
    assert truncated is True


def test_tail_text_caps_large_character_payloads() -> None:
    """Trim very large text blocks before line tailing."""
    text = "a" * 50 + "tail"

    tailed, truncated = _tail_text(text, max_lines=5, max_chars=8)

    assert tailed == "aaaatail"
    assert truncated is True


def test_tail_text_reports_when_text_is_not_truncated() -> None:
    """Leave short text untouched and mark it as fully visible."""
    tailed, truncated = _tail_text("short answer", max_lines=5, max_chars=100)

    assert tailed == "short answer"
    assert truncated is False


def test_render_welcome_includes_agent_and_model() -> None:
    """Render a startup card with the active runtime metadata."""
    runtime = AgentRuntime(
        agent="agent",  # type: ignore[arg-type]
        handler="handler",  # type: ignore[arg-type]
        agent_name="example_agent",
        agent_source="local",
        model="gpt-5.4",
        tools_by_name={},
    )

    console = Console(record=True, width=100)
    console.print(_render_welcome(runtime))
    rendered = console.export_text()

    assert "example_agent" in rendered
    assert "gpt-5.4" in rendered
    assert "coolagents" in rendered
    assert DOG_LOGO.splitlines()[0].strip() in rendered


def test_load_agent_script_registers_code_agents() -> None:
    """Importing a registration script should populate the code agent registry."""
    loader.clear_registered_agents()

    _load_agent_script("/Users/haquangle/workspace/upagent/upup/asianf/examples/agents.py")

    assert "website_analyser" in loader.list_registered_agents()
    assert "news_collector" in loader.list_registered_agents()
