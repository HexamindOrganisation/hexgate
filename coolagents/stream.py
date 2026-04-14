"""Stream and run result datatypes for coolagents."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _new_id() -> str:
    """Return a fresh UUID string."""
    return str(uuid4())


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(UTC)


class EventType(StrEnum):
    """Normalized runtime event types."""

    RUN_START = "run_start"
    BLOCK_START = "block_start"
    BLOCK_DELTA = "block_delta"
    BLOCK_END = "block_end"
    TOOL_START = "tool_start"
    TOOL_UPDATE = "tool_update"
    TOOL_END = "tool_end"
    RUN_END = "run_end"
    ERROR = "error"


class StepType(StrEnum):
    """Persisted step types for a single agent run."""

    TEXT = "text_step"
    REASONING = "reasoning_step"
    TOOL_CALL = "tool_call_step"


class BlockType(StrEnum):
    """Block types emitted during streaming."""

    TEXT = "text"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"


class ToolCallState(StrEnum):
    """Lifecycle states for tool call steps."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class RunNode(BaseModel):
    """Shared ancestry metadata for events and steps."""

    run_id: str
    root_run_id: str
    parent_run_id: str | None = None
    depth: int = 0
    sequence: int


class BaseStep(RunNode):
    """Base model for persisted run steps."""

    id: str = Field(default_factory=_new_id)


class TextStep(BaseStep):
    """Persisted visible assistant text emitted during a run."""

    type: Literal[StepType.TEXT] = StepType.TEXT
    text: str = ""


class ReasoningStep(BaseStep):
    """Persisted reasoning text emitted during a run."""

    type: Literal[StepType.REASONING] = StepType.REASONING
    text: str = ""


class ToolCallStep(BaseStep):
    """Persisted tool call activity emitted during a run."""

    type: Literal[StepType.TOOL_CALL] = StepType.TOOL_CALL
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    state: ToolCallState = ToolCallState.STARTED
    output_summary: str | None = None
    raw_output: Any | None = None


Step = Annotated[TextStep | ReasoningStep | ToolCallStep, Field(discriminator="type")]


class AgentRunResult(BaseModel):
    """Final result for one agent run."""

    run_id: str
    root_run_id: str
    message: str = ""
    steps: list[Step] = Field(default_factory=list)


class BaseStreamEvent(RunNode):
    """Base model for normalized stream events."""

    event_id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_utc_now)


class RunStartEvent(BaseStreamEvent):
    """Signal that a run has started."""

    event_type: Literal[EventType.RUN_START] = EventType.RUN_START
    query: str


class BlockStartEvent(BaseStreamEvent):
    """Signal that a content block has started."""

    event_type: Literal[EventType.BLOCK_START] = EventType.BLOCK_START
    block_id: str
    block_type: BlockType


class BlockDeltaEvent(BaseStreamEvent):
    """Signal that a content block produced streamed text."""

    event_type: Literal[EventType.BLOCK_DELTA] = EventType.BLOCK_DELTA
    block_id: str
    block_type: BlockType
    text: str


class BlockEndEvent(BaseStreamEvent):
    """Signal that a content block has ended."""

    event_type: Literal[EventType.BLOCK_END] = EventType.BLOCK_END
    block_id: str
    block_type: BlockType


class ToolStartEvent(BaseStreamEvent):
    """Signal that a tool execution has started."""

    event_type: Literal[EventType.TOOL_START] = EventType.TOOL_START
    tool_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolUpdateEvent(BaseStreamEvent):
    """Signal that a running tool produced an intermediate update."""

    event_type: Literal[EventType.TOOL_UPDATE] = EventType.TOOL_UPDATE
    tool_id: str
    tool_name: str
    text: str


class ToolEndEvent(BaseStreamEvent):
    """Signal that a tool execution has completed."""

    event_type: Literal[EventType.TOOL_END] = EventType.TOOL_END
    tool_id: str
    tool_name: str
    state: ToolCallState = ToolCallState.COMPLETED
    output_summary: str | None = None


class RunEndEvent(BaseStreamEvent):
    """Signal that a run has completed with a final result."""

    event_type: Literal[EventType.RUN_END] = EventType.RUN_END
    result: AgentRunResult


class ErrorEvent(BaseStreamEvent):
    """Signal that a run or child node has failed."""

    event_type: Literal[EventType.ERROR] = EventType.ERROR
    message: str


StreamEvent = Annotated[
    RunStartEvent
    | BlockStartEvent
    | BlockDeltaEvent
    | BlockEndEvent
    | ToolStartEvent
    | ToolUpdateEvent
    | ToolEndEvent
    | RunEndEvent
    | ErrorEvent,
    Field(discriminator="event_type"),
]
