from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from agents import Agent as OpenAIAgent
from google.adk.agents import Agent as GoogleAgent
from langgraph.graph.state import CompiledStateGraph as LangChainAgent
from pydantic_ai import Agent as PydanticAIAgent

from fortify.agent.factory import CoolAgent as FortifyAgent

AgentType = OpenAIAgent | GoogleAgent | LangChainAgent | PydanticAIAgent | FortifyAgent


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
