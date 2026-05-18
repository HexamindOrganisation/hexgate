from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from fortify.agents.factory import FortifyAgent
from fortify.cli.register.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    ToolDefinition,
)


def create_fortify_manifest(
    agent: FortifyAgent, *, description: str | None = None
) -> AgentManifest:
    """Build an AgentManifest from a Fortify agent created by `create_agent`."""
    if not agent.name:
        raise ValueError(
            "Fortify agent has no name — set a name on the FortifyAgent so the "
            "manifest can identify it on the platform."
        )
    return AgentManifest(
        name=agent.name,
        description=description,
        framework=AgentFramework.FORTIFY,
        tools=[_to_tool_definition(t) for t in agent.tools],
    )


def _to_tool_definition(spec: Any) -> ToolDefinition:
    """Convert a fortify ToolSpec (BaseTool | Callable | dict) to a ToolDefinition."""
    if isinstance(spec, dict):
        return _dict_to_tool_definition(spec)
    tool = spec if isinstance(spec, BaseTool) else StructuredTool.from_function(spec)
    schema = _tool_schema(tool)
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


def _tool_schema(tool: BaseTool) -> dict[str, Any]:
    """Return a JSON schema describing a LangChain tool's arguments."""
    args_schema = tool.args_schema
    if args_schema is None:
        return {}
    if isinstance(args_schema, dict):
        return args_schema
    if hasattr(args_schema, "model_json_schema"):
        return args_schema.model_json_schema()
    return {}


def _dict_to_tool_definition(spec: dict[str, Any]) -> ToolDefinition:
    """Convert an OpenAI-style tool dict to a ToolDefinition."""
    fn = spec.get("function", spec)
    name = fn.get("name") or "tool"
    description = fn.get("description") or ""
    parameters = fn.get("parameters") or {}
    raw_props = parameters.get("properties", {})
    properties = {
        prop_name: InputProperty(
            title=prop.get("title", prop_name),
            type=prop.get("type", "string"),
        )
        for prop_name, prop in raw_props.items()
    }
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=InputSchema(
            properties=properties,
            required=list(parameters.get("required", [])),
        ),
    )
