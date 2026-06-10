"""Conversation and live run state for the hexgate terminal app."""

from __future__ import annotations

from dataclasses import dataclass, field

from hexgate.streaming import (
    AgentRunResult,
    BlockDeltaEvent,
    BlockType,
    ErrorEvent,
    RunEndEvent,
    RunStartEvent,
    StreamEvent,
    ToolCallState,
    ToolEndEvent,
    ToolStartEvent,
)


@dataclass
class TranscriptTurn:
    """Store one rendered chat turn in the terminal transcript."""

    role: str
    content: str


@dataclass
class ToolActivity:
    """Track one tool invocation inside the current run."""

    tool_id: str
    tool_name: str
    status: ToolCallState
    arguments: dict[str, object] = field(default_factory=dict)
    summary: str | None = None


@dataclass
class LiveRunState:
    """Track the currently visible run in the terminal sidebar."""

    query: str
    response_text: str = ""
    reasoning_text: str = ""
    tools: list[ToolActivity] = field(default_factory=list)
    is_streaming: bool = True
    error: str | None = None
    result: AgentRunResult | None = None


class ChatState:
    """Track conversation history and the current live agent run."""

    def __init__(self) -> None:
        """Initialize an empty in-memory chat session."""
        self.messages: list[dict[str, str]] = []
        self.transcript: list[TranscriptTurn] = []
        self.current_run: LiveRunState | None = None

    @property
    def is_busy(self) -> bool:
        """Return whether the agent is currently streaming a response."""
        return bool(self.current_run and self.current_run.is_streaming)

    def start_turn(self, user_text: str) -> None:
        """Append a user turn and initialize the next live run."""
        self.messages.append({"role": "user", "content": user_text})
        self.transcript.append(TranscriptTurn(role="user", content=user_text))
        self.current_run = LiveRunState(query=user_text)

    def clear(self) -> None:
        """Reset the full chat session and transcript."""
        self.messages.clear()
        self.transcript.clear()
        self.current_run = None

    def build_input(self) -> list[dict[str, str]]:
        """Return the current conversation as agent input messages."""
        return list(self.messages)

    def apply_event(self, event: StreamEvent) -> None:
        """Apply one normalized runtime event to the chat session."""
        if isinstance(event, RunStartEvent):
            if self.current_run is None:
                self.current_run = LiveRunState(query=event.query)
            elif not self.current_run.query:
                self.current_run.query = event.query
            return

        if self.current_run is None:
            self.current_run = LiveRunState(query="")

        if isinstance(event, BlockDeltaEvent):
            if event.block_type == BlockType.REASONING:
                self.current_run.reasoning_text += event.text
            else:
                self.current_run.response_text += event.text
            return

        if isinstance(event, ToolStartEvent):
            activity = self._find_tool(event.tool_id)
            if activity is None:
                self.current_run.tools.append(
                    ToolActivity(
                        tool_id=event.tool_id,
                        tool_name=event.tool_name,
                        status=ToolCallState.STARTED,
                        arguments=dict(event.arguments),
                    )
                )
            else:
                activity.tool_name = event.tool_name
                activity.status = ToolCallState.STARTED
                activity.arguments = dict(event.arguments)
            return

        if isinstance(event, ToolEndEvent):
            activity = self._find_tool(event.tool_id)
            if activity is None:
                activity = ToolActivity(
                    tool_id=event.tool_id,
                    tool_name=event.tool_name,
                    status=event.state,
                )
                self.current_run.tools.append(activity)
            activity.status = event.state
            activity.summary = event.output_summary
            return

        if isinstance(event, ErrorEvent):
            self.current_run.error = event.message
            self.current_run.is_streaming = False
            return

        if isinstance(event, RunEndEvent):
            self.current_run.is_streaming = False
            self.current_run.result = event.result
            self.current_run.response_text = event.result.message
            if event.result.message:
                self.messages.append(
                    {"role": "assistant", "content": event.result.message}
                )
                self.transcript.append(
                    TranscriptTurn(role="assistant", content=event.result.message)
                )

    def _find_tool(self, tool_id: str) -> ToolActivity | None:
        """Return a tracked tool activity by identifier when present."""
        if self.current_run is None:
            return None
        for activity in self.current_run.tools:
            if activity.tool_id == tool_id:
                return activity
        return None
