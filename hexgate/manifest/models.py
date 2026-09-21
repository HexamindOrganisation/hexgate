from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

_log = logging.getLogger(__name__)

# Hard cap on the serialized system prompt. The dashboard renders the full
# prompt inside a <pre> block; a multi-MB prompt would lock the browser tab.
# 64 KiB is well above any realistic hand-written prompt and still cheap to
# render. Measured in UTF-8 bytes so the limit is meaningful for non-ASCII
# prompts too.
MAX_SYSTEM_PROMPT_BYTES = 64 * 1024
_TRUNCATION_MARKER = "\n\n… [truncated by hexgate register]"

# A skill library is a directory, so an agent can in principle see hundreds.
# Cap what a manifest carries: the dashboard renders the list, and the platform
# writes one row per skill per version.
MAX_SKILLS = 200
MAX_RESOURCES_PER_SKILL = 100


def _truncate[T](value: list[T], limit: int, label: str) -> list[T]:
    """Cap a list at ``limit``, warning on overflow.

    Truncates rather than raises: a manifest that cannot register is worse than
    one that under-reports.
    """
    if len(value) <= limit:
        return value
    _log.warning(
        "%s: %d entries exceeds the %d cap; truncating", label, len(value), limit
    )
    return value[:limit]


# Enable AgentType type checking, without requiring the agents package to be installed
if TYPE_CHECKING:
    from agents import Agent as OpenAIAgent
    from google.adk.agents import Agent as GoogleAgent
    from langgraph.graph.state import CompiledStateGraph as LangChainAgent
    from pydantic_ai import Agent as PydanticAIAgent

    from hexgate.agents.factory import HexgateAgent as HexgateAgent

    AgentType = (
        OpenAIAgent | GoogleAgent | LangChainAgent | PydanticAIAgent | HexgateAgent
    )
else:
    AgentType = object


class AgentFramework(StrEnum):
    """Enum for the framework of an agent."""

    HEXGATE = "hexgate"
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
            "or otherwise not introspectable at registration time. "
            f"Capped at {MAX_SYSTEM_PROMPT_BYTES // 1024} KiB UTF-8 — anything "
            "longer is truncated with a marker so the dashboard can render "
            "it safely."
        ),
    )
    tools: list[ToolDefinition] = Field(description="The tools of the agent")
    skills: list[SkillDefinition] | None = Field(
        default=None,
        description=(
            "Skills discoverable by this agent, when the framework exposes them. "
            "None — not [] — when the framework has no skill concept, so the "
            "manifest hash is unchanged for every agent that has none."
        ),
    )

    @field_validator("skills")
    @classmethod
    def _cap_skills(
        cls, value: list[SkillDefinition] | None
    ) -> list[SkillDefinition] | None:
        if value is None:
            return None
        return _truncate(value, MAX_SKILLS, "skills")

    @field_validator("system_prompt")
    @classmethod
    def _cap_system_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        encoded = value.encode("utf-8")
        if len(encoded) <= MAX_SYSTEM_PROMPT_BYTES:
            return value
        # Trim by bytes, then decode-ignore to land on a codepoint boundary
        # without splitting a multi-byte sequence. Reserve room for the marker.
        budget = MAX_SYSTEM_PROMPT_BYTES - len(_TRUNCATION_MARKER.encode("utf-8"))
        head = encoded[:budget].decode("utf-8", errors="ignore")
        return head + _TRUNCATION_MARKER


class ToolDefinition(BaseModel):
    """Schema for a tool definition."""

    name: str = Field(description="The name of the tool")
    description: str = Field(description="The description of the tool")
    input_schema: InputSchema = Field(description="The parameters of the tool")


class SkillResources(BaseModel):
    """L3 contents of a skill, by name.

    ``None`` on ``SkillDefinition.resources`` means the framework does not
    enumerate resources at all; an instance with empty lists means it does and
    the skill ships none.
    """

    references: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    scripts: list[str] = Field(default_factory=list)

    @field_validator("references", "assets", "scripts")
    @classmethod
    def _cap(cls, value: list[str]) -> list[str]:
        return _truncate(value, MAX_RESOURCES_PER_SKILL, "skill resources")


class SkillDefinition(BaseModel):
    """One skill available to an agent, as discovered at registration."""

    name: str = Field(description="Skill name, unique within the agent")
    description: str = Field(
        description="What the skill does and when the model should use it"
    )
    source: str | None = Field(
        default=None,
        description="Skill library the skill came from (directory path or label)",
    )
    resources: SkillResources | None = Field(
        default=None,
        description=(
            "L3 contents, when the framework enumerates them. None means it "
            "does not — distinct from an instance with empty lists, which "
            "means the skill ships no resources."
        ),
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description=(
            "agentskills.io 'allowed-tools' frontmatter. Advisory metadata the "
            "frameworks parse but do not enforce — recorded so an operator can "
            "compare it against the policy, never read as a control."
        ),
    )
    additional_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Tools this skill exposes once activated (ADK "
            "'adk_additional_tools'). Non-empty means the agent's callable "
            "tool surface grows at runtime."
        ),
    )
    content_hash: str | None = Field(
        default=None, description="sha256 of the SKILL.md body, for drift detection"
    )


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
