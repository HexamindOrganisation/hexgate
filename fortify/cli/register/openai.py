from __future__ import annotations

from fortify.cli.register.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    ToolDefinition,
)

from agents import Agent
from agents.models.interface import Model
from agents.tool import FunctionTool, Tool


def create_openai_manifest(
    agent: Agent, *, description: str | None = None
) -> AgentManifest:
    """Build an AgentManifest from an OpenAI Agents SDK agent."""
    return AgentManifest(
        name=agent.name,
        description=description,
        framework=AgentFramework.OPENAI,
        model=_extract_model(agent.model),
        system_prompt=_extract_system_prompt(agent.instructions),
        tools=[_to_tool_definition(t) for t in agent.tools],
    )


def _extract_model(model: str | Model | None) -> str | None:
    """Return the model id for an OpenAI Agents SDK agent.

    ``Agent.model`` is ``str | Model | None``
    """
    if model is None:
        return None
    if isinstance(model, str):
        return model or None
    # Fallback to the model class name when no model is set
    return type(model).__name__


def _extract_system_prompt(instructions: object) -> str | None:
    """Return the static instructions string for an OpenAI Agents SDK agent.

    ``Agent.instructions`` can be a string or a callable resolved per run; we
    only snapshot the static string
    """
    if isinstance(instructions, str):
        return instructions or None
    return None


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
