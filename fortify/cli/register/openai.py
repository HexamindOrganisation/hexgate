from __future__ import annotations

from fortify.cli.register.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    ToolDefinition,
)

from agents import Agent
from agents.tool import FunctionTool, Tool


def create_openai_manifest(
    agent: Agent, *, description: str | None = None
) -> AgentManifest:
    """Build an AgentManifest from an OpenAI Agents SDK agent."""
    return AgentManifest(
        name=agent.name,
        description=description,
        framework=AgentFramework.OPENAI,
        tools=[_to_tool_definition(t) for t in agent.tools],
    )


def _to_tool_definition(tool: Tool) -> ToolDefinition:
    if isinstance(tool, FunctionTool):
        schema = tool.params_json_schema or {}
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
    return ToolDefinition(
        name=getattr(tool, "name", type(tool).__name__),
        description="",
        input_schema=InputSchema(properties={}, required=[]),
    )
