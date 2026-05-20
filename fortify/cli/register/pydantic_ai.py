from __future__ import annotations

from fortify.cli.register.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    ToolDefinition,
)

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool


def create_pydantic_ai_manifest(
    agent: Agent, *, description: str | None = None
) -> AgentManifest:
    """Build an AgentManifest from a Pydantic AI agent."""
    if not agent.name:
        raise ValueError(
            "Pydantic AI agent has no name — pass `Agent(name=...)` so the "
            "manifest can identify it on the platform."
        )
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
        model=_extract_model(agent.model),
        system_prompt=_extract_system_prompt(agent),
        tools=tools,
    )


def _extract_model(model: Model | str | None) -> str | None:
    """Return the model id for a Pydantic AI agent.

    ``Model`` exposes ``.model_name`` as the canonical identifier
    """
    if model is None:
        return None
    if isinstance(model, str):
        return model or None
    name = getattr(model, "model_name", None)
    if isinstance(name, str) and name:
        return name
    return type(model).__name__


def _extract_system_prompt(agent: Agent) -> str | None:
    """Collect every static prompt string a Pydantic AI agent will emit.

    Pydantic AI splits the prompt across two private fields: ``_system_prompts``
    and ``_instructions`` (the newer ``instructions=`` arg, which mixes literal
    strings with dynamic callables). At run time the agent concatenates them.
    """
    parts: list[str] = []
    for prompt in getattr(agent, "_system_prompts", ()) or ():
        if isinstance(prompt, str) and prompt:
            parts.append(prompt)
    for instruction in getattr(agent, "_instructions", ()) or ():
        if isinstance(instruction, str) and instruction:
            parts.append(instruction)
    if not parts:
        return None
    return "\n\n".join(parts)


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
