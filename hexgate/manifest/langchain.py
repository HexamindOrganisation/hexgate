from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from hexgate.manifest.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    ToolDefinition,
)

_TOOLS_NODE = "tools"


def create_langchain_manifest(
    graph: CompiledStateGraph,
    tools: list[BaseTool],
    *,
    description: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> AgentManifest:
    """Build an AgentManifest from a LangChain/LangGraph agent.

    ``model`` and ``system_prompt`` are passed explicitly because compiled
    LangGraph graphs do not reliably expose them after compilation. ``tools`` is
    unioned with the graph's bound tools, so middleware-injected tools are
    recorded too.
    """
    agent_name = getattr(graph, "name", None)
    if agent_name is None:
        raise ValueError(
            "LangChain graph has no name — set a name on the graph so the "
            "manifest can identify it on the platform."
        )
    all_tools = _union_tools(tools, discover_graph_tools(graph))
    return AgentManifest(
        name=agent_name,
        description=description,
        framework=AgentFramework.LANGCHAIN,
        model=model,
        system_prompt=system_prompt,
        tools=[_to_tool_definition(t) for t in all_tools],
    )


def discover_graph_tools(graph: CompiledStateGraph) -> list[BaseTool]:
    """Tools bound in a compiled graph's ToolNode, including middleware-injected ones.

    Reads LangGraph internals with no stability promise, so every hop is guarded
    and a graph without a recognisable tools node yields ``[]``.
    """
    nodes = getattr(graph, "nodes", None)
    node = nodes.get(_TOOLS_NODE) if isinstance(nodes, dict) else None
    bound = getattr(node, "bound", None)
    by_name = getattr(bound, "tools_by_name", None)
    return list(by_name.values()) if isinstance(by_name, dict) else []


def _union_tools(
    explicit: list[BaseTool], discovered: list[BaseTool]
) -> list[BaseTool]:
    """Caller's tools first, then discovered tools not already named.

    Order is deterministic because the manifest's tool list feeds its content hash.
    """
    merged = list(explicit)
    seen = {tool.name for tool in merged}
    for tool in discovered:
        if tool.name not in seen:
            merged.append(tool)
            seen.add(tool.name)
    return merged


def _to_tool_definition(tool: BaseTool) -> ToolDefinition:
    """Convert a LangChain/LangGraph tool to a ToolDefinition."""
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
