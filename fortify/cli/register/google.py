from __future__ import annotations

from typing import Any

from fortify.cli.register.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    ToolDefinition,
)

from google.adk.agents import Agent
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool


def create_google_manifest(agent: Agent, *, description: str | None = None) -> AgentManifest:
    """Build an AgentManifest from a Google ADK agent."""

    tools: list[ToolDefinition] = []
    for entry in agent.tools:
        tool = entry if hasattr(entry, "_get_declaration") else FunctionTool(func=entry)
        definition = _to_tool_definition(tool)
        if definition is not None:
            tools.append(definition)
    
    # resolve description from agent or provided description
    description = description or agent.description or None

    return AgentManifest(
        name=agent.name,
        description=description,
        framework=AgentFramework.GOOGLE,
        tools=tools,
    )


def _to_tool_definition(tool: BaseTool) -> ToolDefinition | None:
    """Convert a Google ADK tool to a ToolDefinition."""
    declaration = tool._get_declaration()
    if declaration is None:
        return None

    parameters = declaration.parameters
    raw_properties: dict[str, Any] = dict(parameters.properties or {}) if parameters else {}
    properties = {
        prop_name: InputProperty(
            title=prop_name,
            type=_schema_type(prop),
        )
        for prop_name, prop in raw_properties.items()
    }
    required = list(parameters.required or []) if parameters else []

    return ToolDefinition(
        name=tool.name,
        description=tool.description or "",
        input_schema=InputSchema(properties=properties, required=required),
    )


def _schema_type(schema: Any) -> str:
    """Convert a Google ADK Schema's Type enum to a JSON-Schema type string."""
    type_value = getattr(schema, "type", None)
    if type_value is None:
        return "string"
    raw = getattr(type_value, "value", type_value)
    return str(raw).lower()
