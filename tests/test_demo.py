"""Tests for demo rendering helpers."""

from __future__ import annotations

from asianf.demo import _render_event
from asianf.stream import (
    AgentRunResult,
    BlockDeltaEvent,
    BlockType,
    RunEndEvent,
    ToolCallState,
    ToolEndEvent,
    ToolStartEvent,
)


def test_render_event_formats_text_delta() -> None:
    """Render text block deltas as plain visible text."""
    event = BlockDeltaEvent(
        run_id="run-1",
        root_run_id="run-1",
        sequence=1,
        block_id="block-1",
        block_type=BlockType.TEXT,
        text="Hello",
    )

    assert _render_event(event) == "Hello"


def test_render_event_formats_tool_lifecycle() -> None:
    """Render tool lifecycle events as terminal-friendly status lines."""
    start = ToolStartEvent(
        run_id="tool-run",
        root_run_id="run-1",
        parent_run_id="run-1",
        depth=1,
        sequence=2,
        tool_id="tool-1",
        tool_name="web_search",
    )
    end = ToolEndEvent(
        run_id="tool-run",
        root_run_id="run-1",
        parent_run_id="run-1",
        depth=1,
        sequence=3,
        tool_id="tool-1",
        tool_name="web_search",
        state=ToolCallState.COMPLETED,
    )

    assert _render_event(start) == "\n[tool:start] web_search\n"
    assert _render_event(end) == "[tool:end] web_search\n"


def test_render_event_formats_run_end() -> None:
    """Render run completion as a trailing newline."""
    event = RunEndEvent(
        run_id="run-1",
        root_run_id="run-1",
        sequence=4,
        result=AgentRunResult(run_id="run-1", root_run_id="run-1", message="Done"),
    )

    assert _render_event(event) == "\n"
