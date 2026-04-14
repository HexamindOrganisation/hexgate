"""Tests for terminal chat state helpers."""

from __future__ import annotations

from coolagents.cli.state import ChatState
from coolagents.stream import (
    AgentRunResult,
    BlockDeltaEvent,
    BlockType,
    RunEndEvent,
    ToolCallState,
    ToolEndEvent,
    ToolStartEvent,
)


def test_start_turn_updates_messages_and_transcript() -> None:
    """Track a newly submitted user message in chat state."""
    state = ChatState()

    state.start_turn("hello")

    assert state.messages == [{"role": "user", "content": "hello"}]
    assert len(state.transcript) == 1
    assert state.transcript[0].role == "user"
    assert state.current_run is not None
    assert state.current_run.query == "hello"
    assert state.is_busy is True


def test_apply_event_accumulates_text_and_tool_activity() -> None:
    """Update the live run from streamed text and tool events."""
    state = ChatState()
    state.start_turn("latest ai breakthroughs")

    state.apply_event(
        BlockDeltaEvent(
            run_id="run-1",
            root_run_id="run-1",
            sequence=1,
            block_id="block-1",
            block_type=BlockType.TEXT,
            text="Working on it",
        )
    )
    state.apply_event(
        ToolStartEvent(
            run_id="tool-run",
            root_run_id="run-1",
            parent_run_id="run-1",
            depth=1,
            sequence=2,
            tool_id="tool-1",
            tool_name="web_search",
            arguments={"query": "latest ai breakthroughs"},
        )
    )
    state.apply_event(
        ToolEndEvent(
            run_id="tool-run",
            root_run_id="run-1",
            parent_run_id="run-1",
            depth=1,
            sequence=3,
            tool_id="tool-1",
            tool_name="web_search",
            state=ToolCallState.COMPLETED,
            output_summary="5 results",
        )
    )

    assert state.current_run is not None
    assert state.current_run.response_text == "Working on it"
    assert len(state.current_run.tools) == 1
    assert state.current_run.tools[0].tool_name == "web_search"
    assert state.current_run.tools[0].status == ToolCallState.COMPLETED
    assert state.current_run.tools[0].summary == "5 results"


def test_run_end_appends_assistant_message() -> None:
    """Persist the final assistant message back into the conversation."""
    state = ChatState()
    state.start_turn("hello")

    state.apply_event(
        RunEndEvent(
            run_id="run-1",
            root_run_id="run-1",
            sequence=4,
            result=AgentRunResult(
                run_id="run-1",
                root_run_id="run-1",
                message="Hi there",
            ),
        )
    )

    assert state.is_busy is False
    assert state.messages[-1] == {"role": "assistant", "content": "Hi there"}
    assert state.transcript[-1].role == "assistant"
    assert state.transcript[-1].content == "Hi there"
