from __future__ import annotations

from fortify.cli.register.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    ToolDefinition,
)

from pydantic_ai import Agent
from pydantic_ai.tools import Tool


def create_pydantic_ai_manifest(agent: Agent, *, description: str | None = None) -> AgentManifest:
    """Build an AgentManifest from a Pydantic AI agent."""
    description = description or agent.description or None

    # Build the tool definitions from the toolsets
    tools: list[ToolDefinition] = []
    for toolset in agent.toolsets:
        for tool in getattr(toolset, "tools", {}).values():
            tools.append(_to_tool_definition(tool))

    return AgentManifest(
        name=agent.name,
        description=description,
        framework=AgentFramework.PYDANTIC_AI,
        tools=tools,
    )


def _to_tool_definition(tool: Tool) -> ToolDefinition:
    schema = tool.function_schema.json_schema or {}
    properties = {
        prop_name: InputProperty(
            title=prop.get("title", prop_name),
            type=prop.get("type", "string"),
        )
        for prop_name, prop in schema.get("properties", {}).items()
    }
    return ToolDefinition(
        name=tool.name,
        description=tool.description or "",
        input_schema=InputSchema(
            properties=properties,
            required=list(schema.get("required", [])),
        ),
    )
