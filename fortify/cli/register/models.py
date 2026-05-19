from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

# Enable AgentType type checking, without requiring the agents package to be installed
if TYPE_CHECKING:
    from agents import Agent as OpenAIAgent
    from google.adk.agents import Agent as GoogleAgent
    from langgraph.graph.state import CompiledStateGraph as LangChainAgent
    from pydantic_ai import Agent as PydanticAIAgent

    from fortify.agents.factory import FortifyAgent as FortifyAgent

    AgentType = (
        OpenAIAgent | GoogleAgent | LangChainAgent | PydanticAIAgent | FortifyAgent
    )
else:
    AgentType = object


class AgentFramework(StrEnum):
    """Enum for the framework of an agent."""

    FORTIFY = "fortify"
    PYDANTIC_AI = "pydantic-ai"
    LANGCHAIN = "langchain"
    GOOGLE = "google"
    OPENAI = "openai"


class AgentManifest(BaseModel):
    """Schema for the manifest of an agent."""

    name: str = Field(description="The name of the agent")
    description: str | None = Field(
        default=None, description="The description of the agent"
    )
    framework: AgentFramework = Field(description="The framework of the agent")
    model: str | None = Field(
        default=None,
        description=(
            "Human-readable identifier of the LLM the agent runs on, when the "
            "framework exposes it. Best-effort: None for raw LangGraph graphs "
            "and for callable / runtime-resolved models we cannot stringify."
        ),
    )
    system_prompt: str | None = Field(
        default=None,
        description=(
            "Resolved system prompt text, when the framework exposes a static "
            "one. None when the prompt is a callable, dynamically composed, "
            "or otherwise not introspectable at registration time."
        ),
    )
    tools: list[ToolDefinition] = Field(description="The tools of the agent")


class ToolDefinition(BaseModel):
    """Schema for a tool definition."""

    name: str = Field(description="The name of the tool")
    description: str = Field(description="The description of the tool")
    input_schema: InputSchema = Field(description="The parameters of the tool")


class InputSchema(BaseModel):
    """Schema for a tool's input parameters."""

    properties: dict[str, InputProperty] = Field(
        description="The properties of the tool"
    )
    required: list[str] = Field(description="The required properties of the tool")


class InputProperty(BaseModel):
    """A single property within a tool's input schema."""

    title: str = Field(description="The title of the property")
    type: str = Field(description="The type of the property")
